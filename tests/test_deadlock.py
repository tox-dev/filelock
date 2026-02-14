from __future__ import annotations

import os
import threading
from typing import TYPE_CHECKING

import pytest

from filelock import (
    AsyncFileLock,
    AsyncSoftFileLock,
    BaseAsyncFileLock,
    BaseFileLock,
    FileLock,
    FileLockDeadlockError,
    SoftFileLock,
    Timeout,
)

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.parametrize("lock_type", [FileLock, SoftFileLock])
def test_cross_instance_deadlock_detected(lock_type: type[BaseFileLock], tmp_path: Path) -> None:
    lock_path = tmp_path / "a"
    lock_1 = lock_type(str(lock_path))
    lock_2 = lock_type(str(lock_path))

    with lock_1:
        assert lock_1.is_locked
        with pytest.raises(FileLockDeadlockError, match="would deadlock"):
            lock_2.acquire(timeout=1)


@pytest.mark.parametrize("lock_type", [FileLock, SoftFileLock])
def test_same_instance_reentrant_still_works(lock_type: type[BaseFileLock], tmp_path: Path) -> None:
    lock_path = tmp_path / "a"
    lock = lock_type(str(lock_path))

    with lock:
        assert lock.is_locked
        with lock:
            assert lock.is_locked
            assert lock.lock_counter == 2
        assert lock.is_locked
        assert lock.lock_counter == 1
    assert not lock.is_locked


@pytest.mark.parametrize("lock_type", [FileLock, SoftFileLock])
def test_singleton_avoids_deadlock(lock_type: type[BaseFileLock], tmp_path: Path) -> None:
    lock_path = tmp_path / "a"
    lock_1 = lock_type(str(lock_path), is_singleton=True)
    lock_2 = lock_type(str(lock_path), is_singleton=True)

    assert lock_1 is lock_2

    with lock_1, lock_2:
        assert lock_1.is_locked
        assert lock_1.lock_counter == 2


@pytest.mark.parametrize("lock_type", [FileLock, SoftFileLock])
def test_registry_cleanup_after_release(lock_type: type[BaseFileLock], tmp_path: Path) -> None:
    lock_path = tmp_path / "a"
    lock_1 = lock_type(str(lock_path))
    lock_2 = lock_type(str(lock_path))

    lock_1.acquire()
    lock_1.release()

    lock_2.acquire()
    assert lock_2.is_locked
    lock_2.release()
    assert not lock_2.is_locked


@pytest.mark.parametrize("lock_type", [FileLock, SoftFileLock])
def test_registry_cleanup_after_force_release(lock_type: type[BaseFileLock], tmp_path: Path) -> None:
    lock_path = tmp_path / "a"
    lock_1 = lock_type(str(lock_path))
    lock_2 = lock_type(str(lock_path))

    lock_1.acquire()
    lock_1.acquire()
    assert lock_1.lock_counter == 2

    lock_1.release(force=True)
    assert not lock_1.is_locked

    lock_2.acquire()
    assert lock_2.is_locked
    lock_2.release()


@pytest.mark.parametrize("lock_type", [FileLock, SoftFileLock])
def test_realpath_detection(lock_type: type[BaseFileLock], tmp_path: Path) -> None:
    abs_path = str(tmp_path / "a.lock")
    rel_path = os.path.relpath(abs_path, start=str(tmp_path.parent))
    lock_1 = lock_type(abs_path)
    lock_2 = lock_type(str(tmp_path.parent / rel_path))

    with lock_1, pytest.raises(FileLockDeadlockError):
        lock_2.acquire(timeout=0.1)


@pytest.mark.parametrize("lock_type", [FileLock, SoftFileLock])
def test_different_threads_no_false_positive(lock_type: type[BaseFileLock], tmp_path: Path) -> None:
    lock_path = tmp_path / "a"
    lock_1 = lock_type(str(lock_path))
    lock_2 = lock_type(str(lock_path), timeout=1)
    barrier = threading.Barrier(2)
    results: dict[str, bool] = {}

    def thread_1() -> None:
        with lock_1:
            barrier.wait(timeout=5)
            barrier.wait(timeout=5)
        results["t1_released"] = True

    def thread_2() -> None:
        barrier.wait(timeout=5)
        try:
            lock_2.acquire(timeout=0.1)
        except FileLockDeadlockError:
            results["t2_deadlock_error"] = True
        except Timeout:
            results["t2_timed_out"] = True
        else:
            lock_2.release()
        barrier.wait(timeout=5)

    t1 = threading.Thread(target=thread_1)
    t2 = threading.Thread(target=thread_2)
    t1.start()
    t2.start()
    t1.join(timeout=10)
    t2.join(timeout=10)

    assert results.get("t1_released") is True
    assert "t2_deadlock_error" not in results


@pytest.mark.parametrize("lock_type", [FileLock, SoftFileLock])
def test_different_files_no_false_positive(lock_type: type[BaseFileLock], tmp_path: Path) -> None:
    lock_1 = lock_type(str(tmp_path / "a"))
    lock_2 = lock_type(str(tmp_path / "b"))

    with lock_1, lock_2:
        assert lock_1.is_locked
        assert lock_2.is_locked


@pytest.mark.parametrize("lock_type", [FileLock, SoftFileLock])
def test_deadlock_error_does_not_corrupt_lock_counter(lock_type: type[BaseFileLock], tmp_path: Path) -> None:
    lock_path = tmp_path / "a"
    lock_1 = lock_type(str(lock_path))
    lock_2 = lock_type(str(lock_path))

    with lock_1:
        with pytest.raises(FileLockDeadlockError):
            lock_2.acquire()
        assert lock_2.lock_counter == 0
    assert lock_1.lock_counter == 0


@pytest.mark.parametrize("lock_type", [AsyncFileLock, AsyncSoftFileLock])
@pytest.mark.asyncio
async def test_async_cross_instance_deadlock_detected(lock_type: type[BaseAsyncFileLock], tmp_path: Path) -> None:
    lock_path = tmp_path / "a"
    lock_1 = lock_type(str(lock_path))
    lock_2 = lock_type(str(lock_path))

    async with lock_1:
        assert lock_1.is_locked
        with pytest.raises(FileLockDeadlockError, match="would deadlock"):
            await lock_2.acquire(timeout=1)


@pytest.mark.parametrize("lock_type", [AsyncFileLock, AsyncSoftFileLock])
@pytest.mark.asyncio
async def test_async_registry_cleanup_after_release(lock_type: type[BaseAsyncFileLock], tmp_path: Path) -> None:
    lock_path = tmp_path / "a"
    lock_1 = lock_type(str(lock_path))
    lock_2 = lock_type(str(lock_path))

    await lock_1.acquire()
    await lock_1.release()

    await lock_2.acquire()
    assert lock_2.is_locked
    await lock_2.release()
    assert not lock_2.is_locked


@pytest.mark.parametrize("lock_type", [AsyncFileLock, AsyncSoftFileLock])
@pytest.mark.asyncio
async def test_async_same_instance_reentrant(lock_type: type[BaseAsyncFileLock], tmp_path: Path) -> None:
    lock_path = tmp_path / "a"
    lock = lock_type(str(lock_path))

    async with lock:
        async with lock:
            assert lock.is_locked
            assert lock.lock_counter == 2
        assert lock.is_locked
    assert not lock.is_locked
