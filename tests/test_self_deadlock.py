from __future__ import annotations

import sys
from threading import Thread
from typing import TYPE_CHECKING

import pytest

from filelock import FileLock, SoftFileLock, Timeout

if TYPE_CHECKING:
    from pathlib import Path


unix_only = pytest.mark.skipif(sys.platform == "win32", reason="unix-only symlink test")


@pytest.mark.parametrize("lock_type", [FileLock, SoftFileLock])
def test_same_thread_different_instances_raises(tmp_path: Path, lock_type: type[FileLock]) -> None:
    lock_path = tmp_path / "test.lock"
    lock1 = lock_type(lock_path)
    with lock1:
        lock2 = lock_type(lock_path)
        with pytest.raises(RuntimeError, match="Deadlock"):
            lock2.acquire()


@pytest.mark.parametrize("lock_type", [FileLock, SoftFileLock])
def test_finite_timeout_gives_timeout_not_deadlock(tmp_path: Path, lock_type: type[FileLock]) -> None:
    lock_path = tmp_path / "test.lock"
    lock1 = lock_type(lock_path)
    with lock1:
        lock2 = lock_type(lock_path, timeout=0.1)
        with pytest.raises(Timeout):
            lock2.acquire()


@pytest.mark.parametrize("lock_type", [FileLock, SoftFileLock])
def test_non_blocking_gives_timeout_not_deadlock(tmp_path: Path, lock_type: type[FileLock]) -> None:
    lock_path = tmp_path / "test.lock"
    lock1 = lock_type(lock_path)
    with lock1:
        lock2 = lock_type(lock_path, blocking=False)
        with pytest.raises(Timeout):
            lock2.acquire()


@pytest.mark.parametrize("lock_type", [FileLock, SoftFileLock])
def test_different_paths_no_conflict(tmp_path: Path, lock_type: type[FileLock]) -> None:
    lock1 = lock_type(tmp_path / "a.lock")
    lock2 = lock_type(tmp_path / "b.lock")
    with lock1, lock2:
        assert lock1.is_locked
        assert lock2.is_locked


@pytest.mark.parametrize("lock_type", [FileLock, SoftFileLock])
def test_same_instance_reentrant_works(tmp_path: Path, lock_type: type[FileLock]) -> None:
    lock = lock_type(tmp_path / "test.lock")
    with lock:
        with lock:
            assert lock.is_locked
        assert lock.is_locked
    assert not lock.is_locked


@pytest.mark.parametrize("lock_type", [FileLock, SoftFileLock])
def test_singleton_avoids_deadlock(tmp_path: Path, lock_type: type[FileLock]) -> None:
    lock_path = tmp_path / "test.lock"
    lock1 = lock_type(lock_path, is_singleton=True)
    with lock1:
        lock2 = lock_type(lock_path, is_singleton=True)
        assert lock1 is lock2
        with lock2:
            assert lock2.is_locked


@pytest.mark.parametrize("lock_type", [FileLock, SoftFileLock])
def test_different_threads_no_false_positive(tmp_path: Path, lock_type: type[FileLock]) -> None:
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

    thread = Thread(target=acquire_in_thread)
    thread.start()
    thread.join()
    lock1.release()

    assert not isinstance(error, RuntimeError), "Should not raise RuntimeError in different thread"


@unix_only
@pytest.mark.parametrize("lock_type", [FileLock, SoftFileLock])
def test_symlink_same_canonical_path(tmp_path: Path, lock_type: type[FileLock]) -> None:
    lock_path = tmp_path / "test.lock"
    symlink_path = tmp_path / "link.lock"
    symlink_path.symlink_to(lock_path)

    lock1 = lock_type(lock_path)
    with lock1:
        lock2 = lock_type(symlink_path)
        with pytest.raises(RuntimeError, match="Deadlock"):
            lock2.acquire()


@pytest.mark.parametrize("lock_type", [FileLock, SoftFileLock])
def test_cleanup_on_release(tmp_path: Path, lock_type: type[FileLock]) -> None:
    lock_path = tmp_path / "test.lock"
    lock1 = lock_type(lock_path)
    lock1.acquire()
    lock1.release()

    lock2 = lock_type(lock_path)
    with lock2:
        assert lock2.is_locked


@pytest.mark.parametrize("lock_type", [FileLock, SoftFileLock])
def test_force_release_cleans_registry(tmp_path: Path, lock_type: type[FileLock]) -> None:
    lock_path = tmp_path / "test.lock"
    lock1 = lock_type(lock_path)
    with lock1:
        lock1.acquire()
        assert lock1.lock_counter == 2
    lock1.release(force=True)

    lock2 = lock_type(lock_path)
    with lock2:
        assert lock2.is_locked
