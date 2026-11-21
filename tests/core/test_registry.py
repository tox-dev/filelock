from __future__ import annotations

import threading
from collections import UserDict
from errno import EIO
from typing import TYPE_CHECKING, Final, Literal, NamedTuple

import pytest

from filelock import (
    BaseFileLock,
    FileLock,
    SoftFileLock,
    Timeout,
)
from filelock._core import override
from filelock._core._registry import REGISTRY
from tests.capability_marks import (
    NEEDS_PARENT_SYMLINK_COLLAPSE,
)

if TYPE_CHECKING:
    from pathlib import Path

    from pytest_mock import MockerFixture


@pytest.mark.parametrize("lock_type", [FileLock, SoftFileLock])
def test_same_thread_different_instances_raises(tmp_path: Path, lock_type: type[BaseFileLock]) -> None:
    lock_path = tmp_path / "test.lock"
    with lock_type(lock_path):
        lock2 = lock_type(lock_path)
        with pytest.raises(RuntimeError, match="Deadlock"):
            lock2.acquire()


@pytest.mark.parametrize("lock_type", [FileLock, SoftFileLock])
def test_finite_timeout_gives_timeout_not_deadlock(tmp_path: Path, lock_type: type[BaseFileLock]) -> None:
    lock_path = tmp_path / "test.lock"
    with lock_type(lock_path):
        lock2 = lock_type(lock_path, timeout=0.1)
        with pytest.raises(Timeout):
            lock2.acquire()


@pytest.mark.parametrize("lock_type", [FileLock, SoftFileLock])
def test_infinite_timeout_gives_deadlock_not_hang(tmp_path: Path, lock_type: type[BaseFileLock]) -> None:
    lock_path: Final[Path] = tmp_path / "test.lock"
    with lock_type(lock_path):
        lock: Final[BaseFileLock] = lock_type(lock_path, timeout=float("inf"))
        with pytest.raises(RuntimeError, match="Deadlock"):
            lock.acquire()


@pytest.mark.parametrize("lock_type", [FileLock, SoftFileLock])
def test_non_blocking_gives_timeout_not_deadlock(tmp_path: Path, lock_type: type[BaseFileLock]) -> None:
    lock_path = tmp_path / "test.lock"
    with lock_type(lock_path):
        lock2 = lock_type(lock_path, blocking=False)
        with pytest.raises(Timeout):
            lock2.acquire()


@pytest.mark.parametrize(
    "mode",
    [pytest.param("finite", id="finite"), pytest.param("nonblocking", id="nonblocking")],
)
def test_failed_acquire_keeps_holder_registered(tmp_path: Path, mode: Literal["finite", "nonblocking"]) -> None:
    lock_path = tmp_path / "test.lock"
    with FileLock(lock_path):
        with pytest.raises(Timeout):
            _acquire_for_mode(FileLock(lock_path), mode)
        with pytest.raises(RuntimeError, match="Deadlock"):
            FileLock(lock_path).acquire(cancel_check=lambda: True)


def _acquire_for_mode(lock: BaseFileLock, mode: Literal["finite", "nonblocking"]) -> None:
    if mode == "finite":
        lock.acquire(timeout=0)
    else:
        lock.acquire(blocking=False)


@pytest.mark.parametrize("lock_type", [FileLock, SoftFileLock])
def test_different_paths_no_conflict(tmp_path: Path, lock_type: type[BaseFileLock]) -> None:
    lock1 = lock_type(tmp_path / "a.lock")
    lock2 = lock_type(tmp_path / "b.lock")
    with lock1, lock2:
        assert lock1.is_locked
        assert lock2.is_locked


@pytest.mark.parametrize("lock_type", [FileLock, SoftFileLock])
def test_same_instance_reentrant_works(tmp_path: Path, lock_type: type[BaseFileLock]) -> None:
    lock = lock_type(tmp_path / "test.lock")
    with lock:
        with lock:
            assert lock.is_locked
        assert lock.is_locked
    assert not lock.is_locked


@pytest.mark.parametrize("lock_type", [FileLock, SoftFileLock])
def test_singleton_avoids_deadlock(tmp_path: Path, lock_type: type[BaseFileLock]) -> None:
    lock_path = tmp_path / "test.lock"
    lock1 = lock_type(lock_path, is_singleton=True)
    with lock1:
        lock2 = lock_type(lock_path, is_singleton=True)
        assert lock1 is lock2
        with lock2:
            assert lock2.is_locked


@pytest.mark.parametrize("lock_type", [FileLock, SoftFileLock])
def test_different_threads_no_false_positive(tmp_path: Path, lock_type: type[BaseFileLock]) -> None:
    lock_path = tmp_path / "test.lock"
    lock1 = lock_type(lock_path, timeout=0)
    lock1.acquire()

    error: BaseException | None = None

    def acquire_in_thread() -> None:
        nonlocal error
        lock2 = lock_type(lock_path, timeout=0)
        try:
            lock2.acquire()
        except BaseException as exc:
            error = exc

    thread = threading.Thread(target=acquire_in_thread)
    thread.start()
    thread.join()
    lock1.release()

    assert not isinstance(error, RuntimeError), "Should not raise RuntimeError in different thread"


@NEEDS_PARENT_SYMLINK_COLLAPSE  # pragma: win32 no cover
@pytest.mark.parametrize("lock_type", [FileLock, SoftFileLock])
def test_symlink_same_canonical_path(tmp_path: Path, lock_type: type[BaseFileLock]) -> None:
    # Parent symlinks resolve to one key. The final component remains literal.
    real_dir = tmp_path / "real"
    real_dir.mkdir()
    (tmp_path / "link").symlink_to(real_dir)

    with lock_type(str(real_dir / "test.lock")):
        lock2 = lock_type(str(tmp_path / "link" / "test.lock"))
        with pytest.raises(RuntimeError, match="Deadlock"):
            lock2.acquire()


@NEEDS_PARENT_SYMLINK_COLLAPSE  # pragma: win32 no cover
@pytest.mark.parametrize(
    "depth",
    [pytest.param(1, id="direct"), pytest.param(2, id="nested")],
)
def test_release_drops_acquisition_key_after_parent_retarget(tmp_path: Path, depth: int) -> None:
    lock_path, original_path, replacement_path = _symlinked_lock_paths(tmp_path)
    lock = FileLock(lock_path)
    for _depth in range(depth):
        lock.acquire()
    _retarget_parent(lock_path, replacement_path)

    for _depth in range(depth):
        lock.release()

    with FileLock(original_path) as successor:
        assert successor.is_locked


def _symlinked_lock_paths(tmp_path: Path) -> _SymlinkedPaths:  # pragma: win32 no cover
    original = tmp_path / "original"
    replacement = tmp_path / "replacement"
    original.mkdir()
    replacement.mkdir()
    link = tmp_path / "link"
    link.symlink_to(original, target_is_directory=True)
    return _SymlinkedPaths(link / "test.lock", original / "test.lock", replacement / "test.lock")


class _SymlinkedPaths(NamedTuple):
    lock_path: Path
    original_path: Path
    replacement_path: Path


def _retarget_parent(lock_path: Path, replacement_path: Path) -> None:  # pragma: win32 no cover
    link = lock_path.parent
    link.unlink()
    link.symlink_to(replacement_path.parent, target_is_directory=True)


@NEEDS_PARENT_SYMLINK_COLLAPSE  # pragma: win32 no cover
def test_forced_release_drops_acquisition_key_after_parent_retarget(tmp_path: Path) -> None:
    lock_path, original_path, replacement_path = _symlinked_lock_paths(tmp_path)
    lock = FileLock(lock_path)
    lock.acquire()
    lock.acquire()
    _retarget_parent(lock_path, replacement_path)

    lock.release(force=True)

    with FileLock(original_path) as successor:
        assert successor.is_locked


@NEEDS_PARENT_SYMLINK_COLLAPSE  # pragma: win32 no cover
def test_release_keeps_retargeted_parent_holder_registered(tmp_path: Path) -> None:
    lock_path, _original_path, replacement_path = _symlinked_lock_paths(tmp_path)
    original = FileLock(lock_path)
    original.acquire()
    _retarget_parent(lock_path, replacement_path)
    with FileLock(replacement_path):
        original.release()
        with pytest.raises(RuntimeError, match="Deadlock"):
            FileLock(replacement_path).acquire(cancel_check=lambda: True)


@pytest.mark.parametrize("lock_type", [FileLock, SoftFileLock])
def test_deadlock_registry_cleanup_on_release(tmp_path: Path, lock_type: type[BaseFileLock]) -> None:
    lock_path = tmp_path / "test.lock"
    lock1 = lock_type(lock_path)
    lock1.acquire()
    lock1.release()

    lock2 = lock_type(lock_path)
    with lock2:
        assert lock2.is_locked


@pytest.mark.parametrize("lock_type", [FileLock, SoftFileLock])
def test_force_release_cleans_registry(tmp_path: Path, lock_type: type[BaseFileLock]) -> None:
    lock_path = tmp_path / "test.lock"
    lock1 = lock_type(lock_path)
    with lock1:
        lock1.acquire()
        assert lock1.lock_counter == 2
    lock1.release(force=True)

    lock2 = lock_type(lock_path)
    with lock2:
        assert lock2.is_locked


def test_failed_nested_acquire_keeps_outer_registry_entry(tmp_path: Path, mocker: MockerFixture) -> None:
    lock = FileLock(str(tmp_path / "a"), is_singleton=False)
    lock.acquire()
    with pytest.raises(RuntimeError, match="already held"):
        FileLock(lock.lock_file).acquire(cancel_check=lambda: True)

    mocker.patch.object(lock, "_raise_if_would_deadlock", side_effect=Timeout(lock.lock_file), autospec=True)
    with pytest.raises(Timeout):
        lock.acquire(timeout=0)

    assert lock.lock_counter == 1
    with pytest.raises(RuntimeError, match="already held"):
        FileLock(lock.lock_file).acquire(cancel_check=lambda: True)

    lock.release()


def test_failed_acquire_retains_registration_after_failed_unlock(tmp_path: Path, mocker: MockerFixture) -> None:
    class InterruptedRegistry(UserDict[object, int]):
        interrupted = False

        @override
        def __setitem__(self, key: object, item: int) -> None:
            if not self.interrupted:
                self.interrupted = True
                raise MemoryError
            super().__setitem__(key, item)

    path = tmp_path / "registered.lock"
    lock = FileLock(path)
    mocker.patch.object(REGISTRY, "held", InterruptedRegistry())
    mocker.patch.object(lock, "_release", autospec=True, side_effect=OSError(EIO, "unlock failed"))
    with pytest.raises(BaseExceptionGroup, match="cleanup failed"):
        lock.acquire()

    with pytest.raises(RuntimeError, match="already held"):
        FileLock(path, is_singleton=False).acquire(cancel_check=lambda: True)
    mocker.stopall()
    lock.release()
    with FileLock(path, is_singleton=False, blocking=False) as peer:
        assert peer.is_locked
