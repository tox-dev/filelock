from __future__ import annotations

import subprocess  # ruff:ignore[suspicious-subprocess-import]  # only fixed-argument, no-shell runs below
import sys
import textwrap
import threading
from contextlib import suppress
from itertools import count
from typing import TYPE_CHECKING, Final, Literal

import pytest
from capabilities import CAPABILITIES

from filelock import (
    BaseFileLock,
    FileLock,
    MarkerSoftFileLock,
    SoftFileLease,
    SoftFileLock,
    StrictSoftFileLock,
    Timeout,
)
from tests.core.thread_helpers import THREAD_WAIT

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path
    from types import CodeType, FrameType

    from _typeshed import Unused  # simplint: ignore[private-import-path]  # precise callback type
    from pytest_mock import MockerFixture


@pytest.mark.parametrize("lock_type", [FileLock, SoftFileLock, MarkerSoftFileLock, SoftFileLease])
@pytest.mark.parametrize("thread_local", [pytest.param(True, id="thread_local"), pytest.param(False, id="shared")])
def test_release_interrupted_at_any_call_leaves_the_lock_usable(
    tmp_path: Path, lock_type: type[BaseFileLock], thread_local: bool
) -> None:
    lock_path: Final[str] = str(tmp_path / "a")
    lock = lock_type(lock_path, thread_local=thread_local, timeout=THREAD_WAIT)
    lock.acquire()
    points: Final[int] = _interrupt_points(lock, lock.release)
    for target in range(1, points + 1):
        lock = lock_type(lock_path, thread_local=thread_local, timeout=THREAD_WAIT)
        lock.acquire()
        with pytest.raises(KeyboardInterrupt):
            _interrupted_at_call(lock, target, lock.release)
        lock.release()
        # Bounded first, so a lock the interrupt leaked fails here instead of hanging the blocking acquire below.
        with lock_type(lock_path, blocking=False) as peer:
            assert peer.is_locked
        # A new instance in this thread meets the deadlock check, which a leftover registry entry would trip.
        with lock_type(lock_path) as peer:
            assert peer.is_locked
        lock.acquire()
        lock.release()
    assert points > 0


def _interrupt_points(lock: BaseFileLock, operation: Callable[[], object], *, callee: CodeType | None = None) -> int:
    points: Final[list[None]] = []
    sys.setprofile(_interrupt_hook(lock, callee, lambda: points.append(None)))
    try:
        operation()
    finally:
        sys.setprofile(None)
    return len(points)


def _interrupt_hook(
    lock: BaseFileLock, callee: CodeType | None, on_call: Callable[[], object]
) -> Callable[[FrameType, str, Unused], None]:
    # Interpreters run a profile hook with tracing off, so coverage never records its body.
    # Where the hook would end a resumed generator without its finally, only a fresh call models a signal.
    resumable: Final[bool] = CAPABILITIES["profile-interrupted-generator-finishes"]
    entered: Final[set[FrameType]] = set()

    def hook(frame: FrameType, event: str, _arg: Unused) -> None:  # pragma: no cover  # runs inside the profile hook
        # Injecting signals inside threading's own context managers can leak their locks.
        if event != "call" or (callee is not None and frame.f_code is not callee):
            return
        holder = frame if callee is None else frame.f_back
        if (
            (resumable or frame not in entered)
            and holder is not None
            and any(v is lock for v in holder.f_locals.values())
        ):
            entered.add(frame)
            on_call()

    return hook


def _interrupted_at_call(
    lock: BaseFileLock, target: int, operation: Callable[[], object], *, callee: CodeType | None = None
) -> None:
    calls: Final[count[int]] = count(1)

    def interrupt() -> None:  # pragma: no cover  # runs inside the profile hook, which coverage does not trace
        if next(calls) == target:
            raise KeyboardInterrupt

    sys.setprofile(_interrupt_hook(lock, callee, interrupt))
    try:
        operation()
    finally:
        sys.setprofile(None)


@pytest.mark.parametrize(
    "lock_type",
    [
        pytest.param(FileLock, id="FileLock"),
        pytest.param(SoftFileLock, id="SoftFileLock"),
        pytest.param(MarkerSoftFileLock, id="MarkerSoftFileLock"),
        pytest.param(SoftFileLease, id="SoftFileLease"),
        pytest.param(StrictSoftFileLock, id="StrictSoftFileLock", marks=pytest.mark.requires_hard_links),
    ],
)
@pytest.mark.parametrize("thread_local", [pytest.param(True, id="thread_local"), pytest.param(False, id="shared")])
@pytest.mark.parametrize(
    ("operation", "hold_first"),
    [
        pytest.param("acquire", lambda _lock: None, id="acquire"),
        pytest.param("release", lambda lock: lock.acquire(), id="release"),
    ],
)
def test_interrupted_at_any_call_reports_whether_it_holds(
    tmp_path: Path,
    lock_type: type[BaseFileLock],
    thread_local: bool,
    operation: Literal["acquire", "release"],
    hold_first: Callable[[BaseFileLock], object],
) -> None:
    # Checking before cleanup catches a stale hold that a later release could hide.
    lock = lock_type(tmp_path / "baseline", thread_local=thread_local)
    hold_first(lock)
    points: Final[int] = _interrupt_points(lock, getattr(lock, operation))
    lock.release()
    for target in range(1, points + 1):
        lock_path = tmp_path / str(target)
        lock = lock_type(lock_path, thread_local=thread_local)
        hold_first(lock)
        with pytest.raises(KeyboardInterrupt):
            _interrupted_at_call(lock, target, getattr(lock, operation))
        peer = lock_type(lock_path, timeout=2)
        # A free Windows lock can need a retry to publish its claim.
        with suppress(Timeout):
            peer.acquire(blocking=not lock.is_locked)
        assert peer.is_locked != lock.is_locked
        # Windows leaves a soft marker's unlink that the interrupt cut short to the instance's next call.
        lock.release()
        peer.release()
        with peer:
            assert peer.is_locked
    assert points > 0


@pytest.mark.parametrize(
    "lock_type",
    [
        pytest.param(FileLock, id="FileLock"),
        pytest.param(SoftFileLock, id="SoftFileLock"),
        pytest.param(MarkerSoftFileLock, id="MarkerSoftFileLock"),
        pytest.param(SoftFileLease, id="SoftFileLease"),
        pytest.param(StrictSoftFileLock, id="StrictSoftFileLock", marks=pytest.mark.requires_hard_links),
    ],
)
@pytest.mark.parametrize("thread_local", [pytest.param(True, id="thread_local"), pytest.param(False, id="shared")])
def test_release_interrupted_below_release_completes(
    tmp_path: Path, lock_type: type[BaseFileLock], thread_local: bool
) -> None:
    lock_path: Final[str] = str(tmp_path / "a")
    lock = lock_type(lock_path, thread_local=thread_local)
    lock.acquire()
    points: Final[int] = _interrupt_points(lock, lock.release)
    # Target 1 is the entry of release() itself, which no Python code can cover.
    for target in range(2, points + 1):
        lock = lock_type(lock_path, thread_local=thread_local)
        lock.acquire()
        with pytest.raises(KeyboardInterrupt):
            _interrupted_at_call(lock, target, lock.release)
        assert not lock.is_locked
        with lock_type(lock_path, blocking=False) as peer:
            assert peer.is_locked
    assert points > 1


def test_acquire_interrupted_after_recording_the_hold_releases_it(tmp_path: Path) -> None:
    # Thread registration can receive a signal after the descriptor is marked owned.
    lock_path: Final[str] = str(tmp_path / "a")
    lock = FileLock(lock_path)
    points: Final[int] = _interrupt_points(lock, lock.acquire, callee=threading.current_thread.__code__)
    lock.release()
    for target in range(1, points + 1):
        lock = FileLock(lock_path)
        with pytest.raises(KeyboardInterrupt):
            _interrupted_at_call(lock, target, lock.acquire, callee=threading.current_thread.__code__)
        assert not lock.is_locked
        with FileLock(lock_path, blocking=False) as peer:
            assert peer.is_locked
    assert points > 0


def test_acquire_after_one_interrupted_once_held_excludes_another_process(tmp_path: Path) -> None:
    # A fresh OS acquisition must exclude another process after the interrupted handle closed.
    lock_path: Final[str] = str(tmp_path / "a")
    lock: Final[SoftFileLock] = FileLock(lock_path)
    with pytest.raises(KeyboardInterrupt):
        _interrupted_at_call(lock, 1, lock.acquire, callee=threading.current_thread.__code__)
    with lock:
        script = textwrap.dedent(f"""
            from filelock import FileLock, Timeout
            try:
                FileLock({lock_path!r}).acquire(blocking=False)
            except Timeout:
                print("refused")
        """)
        result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, check=False)
    assert (result.returncode, result.stdout.strip()) == (0, "refused"), result.stderr


@pytest.mark.parametrize(
    "lock_type",
    [
        FileLock,
        SoftFileLock,
        MarkerSoftFileLock,
        SoftFileLease,
        pytest.param(StrictSoftFileLock, id="StrictSoftFileLock", marks=pytest.mark.requires_hard_links),
    ],
)
@pytest.mark.parametrize("thread_local", [pytest.param(True, id="thread-local"), pytest.param(False, id="shared")])
@pytest.mark.parametrize("interrupted", ["_raise_if_would_deadlock", "_pin_lock_path"])
def test_early_acquire_exception_leaves_no_counted_hold(
    tmp_path: Path,
    mocker: MockerFixture,
    lock_type: type[BaseFileLock],
    thread_local: bool,
    interrupted: str,
) -> None:
    lock_path = str(tmp_path / "a.lock")
    lock = lock_type(lock_path, thread_local=thread_local)
    mocker.patch.object(lock, interrupted, side_effect=KeyboardInterrupt, autospec=True)

    with pytest.raises(KeyboardInterrupt):
        lock.acquire()
    mocker.stopall()

    assert (lock.is_locked, lock.lock_counter) == (False, 0)
    with lock:
        assert lock.lock_counter == 1
    assert (lock.is_locked, lock.lock_counter) == (False, 0)
    with lock_type(lock_path, blocking=False, is_singleton=False) as peer:
        assert peer.is_locked


@pytest.mark.parametrize(
    "lock_type",
    [
        FileLock,
        SoftFileLock,
        MarkerSoftFileLock,
        SoftFileLease,
        pytest.param(StrictSoftFileLock, id="StrictSoftFileLock", marks=pytest.mark.requires_hard_links),
    ],
)
def test_early_nested_acquire_exception_keeps_one_level(
    tmp_path: Path, mocker: MockerFixture, lock_type: type[BaseFileLock]
) -> None:
    lock_path = str(tmp_path / "a.lock")
    lock = lock_type(lock_path)
    lock.acquire()
    mocker.patch.object(lock, "_raise_if_would_deadlock", side_effect=KeyboardInterrupt, autospec=True)

    with pytest.raises(KeyboardInterrupt):
        lock.acquire()
    mocker.stopall()

    assert (lock.is_locked, lock.lock_counter) == (True, 1)
    lock.release()
    with lock_type(lock_path, blocking=False, is_singleton=False) as peer:
        assert peer.is_locked


def test_early_acquire_refuses_same_stack_reentry(tmp_path: Path, mocker: MockerFixture) -> None:
    lock = FileLock(str(tmp_path / "a.lock"))

    def reenter(_canonical: str, *, timeout: float, blocking: bool) -> None:
        lock.acquire(timeout=timeout, blocking=blocking)

    mocker.patch.object(lock, "_raise_if_would_deadlock", side_effect=reenter, autospec=True)
    with pytest.raises(RuntimeError, match="acquired again while this thread's acquire of it waits"):
        lock.acquire()
    assert (lock.is_locked, lock.lock_counter) == (False, 0)
