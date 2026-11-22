from __future__ import annotations

import math
import os
import sys
import threading
import time
from decimal import Decimal
from pathlib import Path
from typing import Any, Final, TypedDict
from weakref import WeakValueDictionary

import pytest

from filelock import (
    BaseFileLock,
    FileLock,
    SoftFileLock,
    SoftReadWriteLock,
)
from tests.capability_marks import (
    NEEDS_PARENT_SYMLINK_COLLAPSE,
    NEEDS_PROMPT_FINALIZATION,
    NEEDS_SYMLINK,
)


@pytest.mark.parametrize("lock_type", [FileLock, SoftFileLock])
def test_thread_local_setter_visibility(lock_type: type[BaseFileLock], tmp_path: Path) -> None:
    # A new thread gets the constructor settings when it first accesses the thread-local context.
    lock = lock_type(tmp_path / "x.lock", thread_local=True, poll_interval=0.05)
    lock.poll_interval = 0.5

    observed: list[float] = []

    def read_from_thread() -> None:
        observed.append(lock.poll_interval)

    thread = threading.Thread(target=read_from_thread)
    thread.start()
    thread.join()

    assert observed == [pytest.approx(0.05)]


@pytest.mark.parametrize("lock_type", [FileLock, SoftFileLock])
def test_non_thread_local_setter_visibility(lock_type: type[BaseFileLock], tmp_path: Path) -> None:
    lock = lock_type(tmp_path / "x.lock", thread_local=False, poll_interval=0.05)
    lock.poll_interval = 0.5

    observed: list[float] = []
    thread = threading.Thread(target=lambda: observed.append(lock.poll_interval))
    thread.start()
    thread.join()

    assert observed == [pytest.approx(0.5)]


@pytest.mark.parametrize("lock_type", [FileLock, SoftFileLock])
def test_mode_is_read_only(lock_type: type[BaseFileLock], tmp_path: Path) -> None:
    # Reading mode again distinguishes a read-only property from a missing one.
    lock = lock_type(tmp_path / "x.lock", mode=0o644)

    with pytest.raises(AttributeError):
        lock.mode = 0o600  # ty: ignore[invalid-assignment]  # the absent setter is the contract under test

    assert lock.mode == 0o644


def test_subclass_compatibility(tmp_path: Path) -> None:
    class MyFileLock(FileLock):
        def __init__(
            self,
            lock_file: str | os.PathLike[str],
            timeout: float = -1,
            mode: int = 0o644,
            thread_local: bool = True,
            my_param: int = 0,
            **kwargs: dict[str, Any],  # ruff:ignore[unused-method-argument]  # matches the base constructor signature; extras are dropped
        ) -> None:
            super().__init__(lock_file, timeout, mode, thread_local, is_singleton=True)
            self.blocking = True
            self.my_param = my_param

    lock_path = tmp_path / "a"
    MyFileLock(str(lock_path), my_param=1)

    class MySoftFileLock(SoftFileLock):
        def __init__(
            self,
            lock_file: str | os.PathLike[str],
            timeout: float = -1,
            mode: int = 0o644,
            thread_local: bool = True,
            my_param: int = 0,
            **kwargs: dict[str, Any],  # ruff:ignore[unused-method-argument]  # matches the base constructor signature; extras are dropped
        ) -> None:
            super().__init__(lock_file, timeout, mode, thread_local, blocking=True, is_singleton=True)
            self.my_param = my_param

    MySoftFileLock(str(lock_path), my_param=1)


@pytest.mark.parametrize("lock_type", [FileLock, SoftFileLock])
def test_singleton_and_non_singleton_locks_are_distinct(lock_type: type[BaseFileLock], tmp_path: Path) -> None:
    lock_path = tmp_path / "a"
    lock_1 = lock_type(str(lock_path), is_singleton=False)
    assert lock_1.is_singleton is False

    lock_2 = lock_type(str(lock_path), is_singleton=True)
    assert lock_2.is_singleton is True
    assert lock_2 is not lock_1


@pytest.mark.parametrize("lock_type", [FileLock, SoftFileLock])
def test_singleton_locks_are_the_same(lock_type: type[BaseFileLock], tmp_path: Path) -> None:
    lock_path = tmp_path / "a"
    lock_1 = lock_type(str(lock_path), is_singleton=True)

    lock_2 = lock_type(str(lock_path), is_singleton=True)
    assert lock_2 is lock_1


def test_singleton_locks_survive_concurrent_first_construction(tmp_path: Path) -> None:
    # A slow constructor exposes the gap between a cache miss and storing the instance.
    lock_path = tmp_path / "a"

    class _SlowLock(SoftFileLock):
        def __init__(self, lock_file: str, *, is_singleton: bool = True) -> None:
            time.sleep(0.05)  # simplint: ignore[sleep-in-test]  # needs a construction-start hook
            super().__init__(lock_file, is_singleton=is_singleton)

    results: list[BaseFileLock] = []
    barrier = threading.Barrier(2)

    def build() -> None:
        barrier.wait()
        results.append(_SlowLock(str(lock_path), is_singleton=True))

    threads = [threading.Thread(target=build) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert results[0] is results[1]


@pytest.mark.parametrize("lock_type", [FileLock, SoftFileLock])
def test_singleton_locks_are_distinct_per_lock_file(lock_type: type[BaseFileLock], tmp_path: Path) -> None:
    lock_1 = lock_type(str(tmp_path / "a"), is_singleton=True)

    lock_2 = lock_type(str(tmp_path / "b"), is_singleton=True)
    assert lock_1 is not lock_2


class _SingletonArgs(TypedDict):
    timeout: float
    mode: int
    thread_local: bool
    blocking: bool
    poll_interval: float


_SINGLETON_ARGS: Final[_SingletonArgs] = _SingletonArgs(
    timeout=-1, mode=0o644, thread_local=True, blocking=True, poll_interval=0.05
)


@pytest.mark.parametrize("lock_type", [FileLock, SoftFileLock])
@pytest.mark.parametrize(
    ("arg_name", "alternate"),
    [
        pytest.param("timeout", 10, id="timeout"),
        pytest.param("mode", 0o600, id="mode"),
        pytest.param("thread_local", False, id="thread-local"),
        pytest.param("blocking", False, id="blocking"),
        pytest.param("poll_interval", 0.1, id="poll-interval"),
    ],
)
def test_singleton_locks_must_be_initialized_with_the_same_args(
    lock_type: type[BaseFileLock], tmp_path: Path, arg_name: str, alternate: float | bool
) -> None:
    lock_path = tmp_path / "a"
    lock = lock_type(str(lock_path), is_singleton=True, **_SINGLETON_ARGS)

    altered_args = _SINGLETON_ARGS | {arg_name: alternate}
    with pytest.raises(
        ValueError, match="Singleton lock instances cannot be initialized with differing arguments"
    ) as exc_info:
        lock_type(str(lock_path), is_singleton=True, **altered_args)
    exc_info.match(arg_name)
    del lock


@pytest.mark.parametrize(
    ("first", "second"),
    [
        pytest.param(-1, math.inf, id="inf"),
        pytest.param(-1, -5, id="other-negative"),
        pytest.param(math.nan, math.nan, id="nan"),
        pytest.param(Decimal("0.1"), Decimal("0.1"), id="decimal"),
    ],
)
def test_singleton_accepts_a_timeout_that_waits_the_same(first: float, second: float, tmp_path: Path) -> None:
    lock: Final[SoftFileLock] = FileLock(tmp_path / "a", is_singleton=True, timeout=first)
    assert FileLock(tmp_path / "a", is_singleton=True, timeout=second) is lock


@NEEDS_PROMPT_FINALIZATION
@pytest.mark.parametrize("lock_type", [FileLock, SoftFileLock])
def test_singleton_locks_are_deleted_when_no_external_references_exist(
    lock_type: type[BaseFileLock],
    tmp_path: Path,
) -> None:
    lock = lock_type(str(tmp_path / "a"), is_singleton=True)

    assert list(lock_type._instances.values()) == [lock]
    del lock
    assert lock_type._instances == {}


@pytest.mark.parametrize("lock_type", [FileLock, SoftFileLock])
def test_singleton_instance_tracking_is_unique_per_subclass(lock_type: type[BaseFileLock]) -> None:
    class Lock1(lock_type):  # ty: ignore[unsupported-base]  # ty types the parametrized base as type[BaseFileLock], not a class
        pass

    class Lock2(lock_type):  # ty: ignore[unsupported-base]  # ty types the parametrized base as type[BaseFileLock], not a class
        pass

    assert isinstance(Lock1._instances, WeakValueDictionary)
    assert isinstance(Lock2._instances, WeakValueDictionary)
    assert Lock1._instances is not Lock2._instances


def test_singleton_locks_when_inheriting_init_is_called_once(tmp_path: Path) -> None:
    init_calls = 0

    class MyFileLock(FileLock):
        def __init__(self, *args: Any, **kwargs: Any) -> None:  # ruff:ignore[any-type]  # forwards arbitrary constructor arguments to super().__init__
            super().__init__(*args, **kwargs)
            nonlocal init_calls
            init_calls += 1

    lock_path = tmp_path / "a"
    lock1 = MyFileLock(str(lock_path), is_singleton=True)
    lock2 = MyFileLock(str(lock_path), is_singleton=True)

    assert lock1 is lock2
    assert init_calls == 1


def test_singleton_shares_across_equivalent_spellings(tmp_path: Path) -> None:
    (tmp_path / "sub").mkdir()
    absolute = FileLock(str(tmp_path / "a"), is_singleton=True)
    dot = FileLock(f"{tmp_path!s}/./a", is_singleton=True)
    dotdot = FileLock(f"{tmp_path / 'sub'!s}/../a", is_singleton=True)
    try:
        assert absolute is dot is dotdot
    finally:
        absolute.release(force=True)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows drive letters are case-insensitive")  # pragma: win32 cover
@pytest.mark.parametrize("lock_type", [FileLock, SoftFileLock])
def test_singleton_shares_across_windows_drive_case(tmp_path: Path, lock_type: type[BaseFileLock]) -> None:
    lock_path = tmp_path / "Resource.lock"
    lock_path.touch()
    drive, tail = os.path.splitdrive(lock_path)
    alternate = Path(drive.swapcase() + tail)
    assert alternate.samefile(lock_path)
    lock_path.unlink()
    original = lock_type(lock_path, is_singleton=True)
    other = lock_type(alternate, is_singleton=True)
    try:
        assert original is other
        with original, other.acquire(timeout=0):
            assert original.lock_counter == 2
    finally:
        original.release(force=True)
        other.release(force=True)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows drive letters are case-insensitive")  # pragma: win32 cover
@pytest.mark.parametrize("lock_type", [FileLock, SoftFileLock])
def test_windows_drive_case_does_not_bypass_deadlock_detection(tmp_path: Path, lock_type: type[BaseFileLock]) -> None:
    lock_path = tmp_path / "Resource.lock"
    drive, tail = os.path.splitdrive(lock_path)
    with lock_type(lock_path):
        other = lock_type(drive.swapcase() + tail)
        with pytest.raises(RuntimeError, match="Deadlock"):
            other.acquire(cancel_check=lambda: True)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows drive letters are case-insensitive")  # pragma: win32 cover
def test_soft_rw_singleton_shares_across_windows_drive_case(tmp_path: Path) -> None:
    lock_path = tmp_path / "Resource.lock"
    drive, tail = os.path.splitdrive(lock_path)
    original = SoftReadWriteLock(lock_path)
    assert SoftReadWriteLock(drive.swapcase() + tail) is original


@NEEDS_PARENT_SYMLINK_COLLAPSE  # pragma: win32 no cover
def test_singleton_shares_through_symlinked_parent(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    (tmp_path / "link").symlink_to(real)
    via_real = FileLock(str(real / "a"), is_singleton=True)
    via_link = FileLock(str(tmp_path / "link" / "a"), is_singleton=True)
    try:
        assert via_real is via_link
    finally:
        via_real.release(force=True)


@NEEDS_SYMLINK  # pragma: needs symlink
def test_final_symlink_stays_a_distinct_key(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.write_text("")
    (tmp_path / "link").symlink_to(target)
    on_link = FileLock(str(tmp_path / "link"), is_singleton=True)
    on_target = FileLock(str(target), is_singleton=True)
    try:
        assert on_link is not on_target  # the final component is not resolved, so the symlink is its own key
    finally:
        on_link.release(force=True)
        on_target.release(force=True)


def test_separate_lock_classes_keep_separate_registries(tmp_path: Path) -> None:
    path = str(tmp_path / "a")
    native = FileLock(path, is_singleton=True)
    soft = SoftFileLock(path, is_singleton=True)
    try:
        assert native is not soft
    finally:
        native.release(force=True)
        soft.release(force=True)
