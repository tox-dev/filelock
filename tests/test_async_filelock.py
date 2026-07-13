from __future__ import annotations

import asyncio
import gc
import logging
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from errno import EIO
from pathlib import Path, PurePath
from typing import TYPE_CHECKING

import pytest

from filelock import AsyncFileLock, AsyncSoftFileLock, BaseAsyncFileLock, Timeout

if TYPE_CHECKING:
    from pytest_mock import MockerFixture

_UNIX_FLOCK_ONLY = pytest.mark.skipif(sys.platform == "win32", reason="native flock semantics are Unix-only")


@pytest.mark.parametrize("lock_type", [AsyncFileLock, AsyncSoftFileLock])
@pytest.mark.parametrize("path_type", [str, PurePath, Path])
@pytest.mark.parametrize("filename", ["a", "new/b", "new2/new3/c"])
@pytest.mark.asyncio
async def test_simple(
    lock_type: type[BaseAsyncFileLock],
    path_type: type[str | Path],
    filename: str,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)

    lock_path = tmp_path / filename
    lock = lock_type(path_type(lock_path))
    async with lock as locked:
        assert lock.is_locked
        assert lock is locked
    assert not lock.is_locked

    assert caplog.messages == [
        f"Attempting to acquire lock {id(lock)} on {lock_path}",
        f"Lock {id(lock)} acquired on {lock_path}",
        f"Attempting to release lock {id(lock)} on {lock_path}",
        f"Lock {id(lock)} released on {lock_path}",
    ]
    assert [r.levelno for r in caplog.records] == [logging.DEBUG, logging.DEBUG, logging.DEBUG, logging.DEBUG]
    assert [r.name for r in caplog.records] == ["filelock", "filelock", "filelock", "filelock"]
    assert logging.getLogger("filelock").level == logging.NOTSET


@pytest.mark.parametrize("lock_type", [AsyncFileLock, AsyncSoftFileLock])
@pytest.mark.parametrize("path_type", [str, PurePath, Path])
@pytest.mark.parametrize("filename", ["a", "new/b", "new2/new3/c"])
@pytest.mark.asyncio
async def test_acquire(
    lock_type: type[BaseAsyncFileLock],
    path_type: type[str | Path],
    filename: str,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)

    lock_path = tmp_path / filename
    lock = lock_type(path_type(lock_path))
    async with await lock.acquire() as locked:
        assert lock.is_locked
        assert lock is locked
    assert not lock.is_locked

    assert caplog.messages == [
        f"Attempting to acquire lock {id(lock)} on {lock_path}",
        f"Lock {id(lock)} acquired on {lock_path}",
        f"Attempting to release lock {id(lock)} on {lock_path}",
        f"Lock {id(lock)} released on {lock_path}",
    ]
    assert [r.levelno for r in caplog.records] == [logging.DEBUG, logging.DEBUG, logging.DEBUG, logging.DEBUG]
    assert [r.name for r in caplog.records] == ["filelock", "filelock", "filelock", "filelock"]
    assert logging.getLogger("filelock").level == logging.NOTSET


@pytest.mark.parametrize("lock_type", [AsyncFileLock, AsyncSoftFileLock])
@pytest.mark.asyncio
async def test_non_blocking(lock_type: type[BaseAsyncFileLock], tmp_path: Path) -> None:
    lock_path = tmp_path / "a"
    lock_1, lock_2 = lock_type(str(lock_path)), lock_type(str(lock_path))
    lock_3 = lock_type(str(lock_path), blocking=False)
    lock_4 = lock_type(str(lock_path), timeout=0)
    lock_5 = lock_type(str(lock_path), blocking=False, timeout=-1)

    await lock_1.acquire()
    assert lock_1.is_locked
    assert not lock_2.is_locked
    assert not lock_3.is_locked
    assert not lock_4.is_locked
    assert not lock_5.is_locked

    with pytest.raises(Timeout, match=r"The file lock '.*' could not be acquired."):
        await lock_2.acquire(blocking=False)
    assert not lock_2.is_locked
    assert lock_1.is_locked

    with pytest.raises(Timeout, match=r"The file lock '.*' could not be acquired."):
        await lock_3.acquire()
    assert not lock_3.is_locked
    assert lock_1.is_locked

    with pytest.raises(Timeout, match=r"The file lock '.*' could not be acquired."):
        async with lock_3:
            pass
    assert not lock_3.is_locked
    assert lock_1.is_locked

    with pytest.raises(Timeout, match=r"The file lock '.*' could not be acquired."):
        await lock_4.acquire()
    assert not lock_4.is_locked
    assert lock_1.is_locked

    with pytest.raises(Timeout, match=r"The file lock '.*' could not be acquired."):
        async with lock_4:
            pass
    assert not lock_4.is_locked
    assert lock_1.is_locked

    # blocking precedence over timeout
    with pytest.raises(Timeout, match=r"The file lock '.*' could not be acquired."):
        await lock_5.acquire()
    assert not lock_5.is_locked
    assert lock_1.is_locked

    with pytest.raises(Timeout, match=r"The file lock '.*' could not be acquired."):
        async with lock_5:
            pass
    assert not lock_5.is_locked
    assert lock_1.is_locked

    await lock_1.release()
    assert not lock_1.is_locked
    assert not lock_2.is_locked
    assert not lock_3.is_locked
    assert not lock_4.is_locked
    assert not lock_5.is_locked


@pytest.mark.parametrize("lock_type", [AsyncFileLock, AsyncSoftFileLock])
@pytest.mark.parametrize("thread_local", [True, False])
@pytest.mark.asyncio
async def test_non_executor(lock_type: type[BaseAsyncFileLock], thread_local: bool, tmp_path: Path) -> None:
    lock = lock_type(str(tmp_path / "a"), thread_local=thread_local, run_in_executor=False)
    async with lock as locked:
        assert lock.is_locked
        assert lock is locked
    assert not lock.is_locked


@pytest.mark.asyncio
async def test_coroutine_function(tmp_path: Path) -> None:
    acquired = released = False

    class AioFileLock(BaseAsyncFileLock):
        async def _acquire(self) -> None:  # ty: ignore[invalid-method-override]
            nonlocal acquired
            acquired = True
            self._context.lock_file_fd = 1

        async def _release(self) -> None:  # ty: ignore[invalid-method-override]
            nonlocal released
            released = True
            self._context.lock_file_fd = None

    lock = AioFileLock(str(tmp_path / "a"))
    await lock.acquire()
    assert acquired
    assert not released
    await lock.release()
    assert acquired
    assert released


@pytest.mark.parametrize("lock_type", [AsyncFileLock, AsyncSoftFileLock])
@pytest.mark.asyncio
async def test_wait_message_logged(
    lock_type: type[BaseAsyncFileLock], tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    lock_path = tmp_path / "a"
    first_lock = lock_type(str(lock_path))
    second_lock = lock_type(str(lock_path), timeout=0.2)

    # Hold the lock so second_lock has to wait
    await first_lock.acquire()
    with pytest.raises(Timeout):
        await second_lock.acquire()
    assert any("waiting" in msg for msg in caplog.messages)


@pytest.mark.parametrize("lock_type", [AsyncSoftFileLock, AsyncFileLock])
@pytest.mark.asyncio
async def test_attempting_to_acquire_branch(
    lock_type: type[BaseAsyncFileLock], tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)

    lock = lock_type(str(tmp_path / "a"))
    await lock.acquire()
    assert any("Attempting to acquire lock" in m for m in caplog.messages)
    await lock.release()


@pytest.mark.asyncio
async def test_thread_local_run_in_executor(tmp_path: Path) -> None:  # noqa: RUF029
    with pytest.raises(ValueError, match="run_in_executor is not supported when thread_local is True"):
        AsyncSoftFileLock(str(tmp_path / "a"), thread_local=True, run_in_executor=True)


@pytest.mark.parametrize("lock_type", [AsyncSoftFileLock, AsyncFileLock])
@pytest.mark.asyncio
async def test_attempting_to_acquire(
    lock_type: type[BaseAsyncFileLock], tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    lock = lock_type(str(tmp_path / "a.lock"), run_in_executor=False)
    await lock.acquire(timeout=0.1)
    assert any("Attempting to acquire lock" in m for m in caplog.messages)
    await lock.release()


@pytest.mark.parametrize("lock_type", [AsyncSoftFileLock, AsyncFileLock])
@pytest.mark.asyncio
async def test_attempting_to_release(
    lock_type: type[BaseAsyncFileLock], tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    lock = lock_type(str(tmp_path / "a.lock"), run_in_executor=False)

    await lock.acquire(timeout=0.1)
    await lock.acquire(timeout=0.1)  # reentrant acquire
    await lock.release(force=True)

    assert any("Attempting to release lock" in m for m in caplog.messages)
    assert any("released" in m for m in caplog.messages)


@pytest.mark.parametrize("lock_type", [AsyncFileLock, AsyncSoftFileLock])
@pytest.mark.asyncio
async def test_release_early_exit_when_unlocked(lock_type: type[BaseAsyncFileLock], tmp_path: Path) -> None:
    lock = lock_type(str(tmp_path / "a.lock"), run_in_executor=False)
    assert not lock.is_locked
    await lock.release()
    assert not lock.is_locked


@pytest.mark.parametrize("lock_type", [AsyncFileLock, AsyncSoftFileLock])
@pytest.mark.asyncio
async def test_release_nonzero_counter_exit(
    lock_type: type[BaseAsyncFileLock], tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    lock = lock_type(str(tmp_path / "a.lock"), run_in_executor=False)
    await lock.acquire()
    await lock.acquire()
    await lock.release()
    assert lock.lock_counter == 1
    assert lock.is_locked
    assert not any("Attempting to release" in m for m in caplog.messages)
    await lock.release()


@pytest.mark.parametrize("lock_type", [AsyncFileLock, AsyncSoftFileLock])
@pytest.mark.asyncio
async def test_cancel_check_triggers(lock_type: type[BaseAsyncFileLock], tmp_path: Path) -> None:
    lock_path = tmp_path / "a"
    lock_1 = lock_type(str(lock_path))
    lock_2 = lock_type(str(lock_path))

    await lock_1.acquire()

    with pytest.raises(Timeout, match=r"The file lock '.*' could not be acquired."):
        await lock_2.acquire(timeout=1, cancel_check=lambda: True)
    assert not lock_2.is_locked
    await lock_1.release()


@pytest.mark.parametrize("lock_type", [AsyncFileLock, AsyncSoftFileLock])
@pytest.mark.asyncio
async def test_cancel_check_not_called_when_lock_available(lock_type: type[BaseAsyncFileLock], tmp_path: Path) -> None:
    lock = lock_type(str(tmp_path / "a"))

    called = False

    def should_not_be_called() -> bool:
        nonlocal called
        called = True
        return True

    await lock.acquire(cancel_check=should_not_be_called)
    assert lock.is_locked
    assert not called
    await lock.release()


@pytest.mark.parametrize("lock_type", [AsyncFileLock, AsyncSoftFileLock])
@pytest.mark.asyncio
async def test_cancel_check_log_message(
    lock_type: type[BaseAsyncFileLock], tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    lock_path = tmp_path / "a"
    lock_1 = lock_type(str(lock_path))
    lock_2 = lock_type(str(lock_path))

    await lock_1.acquire()
    with pytest.raises(Timeout):
        await lock_2.acquire(timeout=1, cancel_check=lambda: True)
    assert any("Cancellation requested" in msg for msg in caplog.messages)
    await lock_1.release()


@pytest.mark.parametrize(
    "lock_type",
    [pytest.param(AsyncFileLock, id="async"), pytest.param(AsyncSoftFileLock, id="soft")],
)
def test_sync_with_raises_not_implemented_error(lock_type: type[BaseAsyncFileLock], tmp_path: Path) -> None:
    # __exit__ must exist so Python can call it after __enter__ raises; without it AttributeError hides the real error
    with pytest.raises(NotImplementedError, match=r"async with"), lock_type(str(tmp_path / "test.lock")):
        pass  # pragma: no cover


@pytest.mark.parametrize(
    "lock_type",
    [pytest.param(AsyncFileLock, id="async"), pytest.param(AsyncSoftFileLock, id="soft")],
)
def test_del_after_loop_close_does_not_raise(lock_type: type[BaseAsyncFileLock], tmp_path: Path) -> None:
    # __del__ must not call get_running_loop(); it raises RuntimeError when no loop is running
    def _run() -> None:
        lock = lock_type(str(tmp_path / "test.lock"))
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(lock.acquire())
            loop.run_until_complete(lock.release(force=True))
        finally:
            loop.close()
            asyncio.set_event_loop(None)
        del lock
        gc.collect()

    with ThreadPoolExecutor(max_workers=1) as pool:
        pool.submit(_run).result(timeout=10)


@pytest.mark.parametrize("lock_type", [AsyncFileLock, AsyncSoftFileLock])
@pytest.mark.asyncio
async def test_same_task_different_instances_raises(tmp_path: Path, lock_type: type[BaseAsyncFileLock]) -> None:
    lock_path = tmp_path / "test.lock"
    lock1 = lock_type(lock_path)
    async with lock1:
        lock2 = lock_type(lock_path)
        with pytest.raises(RuntimeError, match="Deadlock"):
            await lock2.acquire()


@pytest.mark.parametrize("lock_type", [AsyncFileLock, AsyncSoftFileLock])
@pytest.mark.asyncio
async def test_finite_timeout_gives_timeout_not_deadlock(tmp_path: Path, lock_type: type[BaseAsyncFileLock]) -> None:
    lock_path = tmp_path / "test.lock"
    lock1 = lock_type(lock_path)
    async with lock1:
        lock2 = lock_type(lock_path, timeout=0.1)
        with pytest.raises(Timeout):
            await lock2.acquire()


@pytest.mark.parametrize("lock_type", [AsyncFileLock, AsyncSoftFileLock])
@pytest.mark.asyncio
async def test_non_blocking_gives_timeout_not_deadlock(tmp_path: Path, lock_type: type[BaseAsyncFileLock]) -> None:
    lock_path = tmp_path / "test.lock"
    lock1 = lock_type(lock_path)
    async with lock1:
        lock2 = lock_type(lock_path, blocking=False)
        with pytest.raises(Timeout):
            await lock2.acquire()


@pytest.mark.parametrize("lock_type", [AsyncFileLock, AsyncSoftFileLock])
@pytest.mark.asyncio
async def test_different_paths_no_conflict(tmp_path: Path, lock_type: type[BaseAsyncFileLock]) -> None:
    lock1 = lock_type(tmp_path / "a.lock")
    lock2 = lock_type(tmp_path / "b.lock")
    async with lock1, lock2:
        assert lock1.is_locked
        assert lock2.is_locked


@pytest.mark.parametrize("lock_type", [AsyncFileLock, AsyncSoftFileLock])
@pytest.mark.asyncio
async def test_same_instance_reentrant_works(tmp_path: Path, lock_type: type[BaseAsyncFileLock]) -> None:
    lock = lock_type(tmp_path / "test.lock")
    async with lock:
        async with lock:
            assert lock.is_locked
        assert lock.is_locked
    assert not lock.is_locked


@pytest.mark.parametrize("lock_type", [AsyncFileLock, AsyncSoftFileLock])
@pytest.mark.asyncio
async def test_singleton_avoids_deadlock(tmp_path: Path, lock_type: type[BaseAsyncFileLock]) -> None:
    lock_path = tmp_path / "test.lock"
    lock1 = lock_type(lock_path, is_singleton=True)
    async with lock1:
        lock2 = lock_type(lock_path, is_singleton=True)
        assert lock1 is lock2
        async with lock2:
            assert lock2.is_locked


@pytest.mark.parametrize("lock_type", [AsyncFileLock, AsyncSoftFileLock])
@pytest.mark.asyncio
async def test_different_tasks_no_false_positive(tmp_path: Path, lock_type: type[BaseAsyncFileLock]) -> None:
    # The registry is per-thread, so a second acquire from a different task must not look like a reentrant deadlock.
    lock_path = tmp_path / "test.lock"
    lock1 = lock_type(lock_path, timeout=0)
    await lock1.acquire()

    error: BaseException | None = None

    async def acquire_other() -> None:
        nonlocal error
        lock2 = lock_type(lock_path, timeout=0)
        try:
            await lock2.acquire()
        except BaseException as exc:
            error = exc

    await asyncio.create_task(acquire_other())
    await lock1.release()

    assert not isinstance(error, RuntimeError), "Should not raise RuntimeError in a different task"


@pytest.mark.skipif(sys.platform == "win32", reason="unix-only symlink test")
@pytest.mark.parametrize("lock_type", [AsyncFileLock, AsyncSoftFileLock])
@pytest.mark.asyncio
async def test_symlink_same_canonical_path(tmp_path: Path, lock_type: type[BaseAsyncFileLock]) -> None:
    lock_path = tmp_path / "test.lock"
    symlink_path = tmp_path / "link.lock"
    symlink_path.symlink_to(lock_path)

    lock1 = lock_type(lock_path)
    async with lock1:
        lock2 = lock_type(symlink_path)
        with pytest.raises(RuntimeError, match="Deadlock"):
            await lock2.acquire()


@pytest.mark.parametrize("lock_type", [AsyncFileLock, AsyncSoftFileLock])
@pytest.mark.asyncio
async def test_deadlock_registry_cleanup_on_release(tmp_path: Path, lock_type: type[BaseAsyncFileLock]) -> None:
    lock_path = tmp_path / "test.lock"
    lock1 = lock_type(lock_path)
    await lock1.acquire()
    await lock1.release()

    lock2 = lock_type(lock_path)
    async with lock2:
        assert lock2.is_locked


@pytest.mark.parametrize("lock_type", [AsyncFileLock, AsyncSoftFileLock])
@pytest.mark.asyncio
async def test_force_release_clears_registry(tmp_path: Path, lock_type: type[BaseAsyncFileLock]) -> None:
    lock_path = tmp_path / "test.lock"
    lock1 = lock_type(lock_path)
    async with lock1:
        await lock1.acquire()
        assert lock1.lock_counter == 2
    await lock1.release(force=True)

    lock2 = lock_type(lock_path)
    async with lock2:
        assert lock2.is_locked


@_UNIX_FLOCK_ONLY
@pytest.mark.asyncio
async def test_release_keeps_lock_held_when_unlock_fails(tmp_path: Path, mocker: MockerFixture) -> None:
    lock = AsyncFileLock(str(tmp_path / "a"))
    await lock.acquire()
    mocker.patch("filelock._unix.fcntl.flock", side_effect=[OSError(EIO, "unlock failed"), None])
    with pytest.raises(OSError, match="unlock failed"):
        await lock.release()
    assert lock.is_locked
    assert lock.lock_counter == 1
    await lock.release()
    assert not lock.is_locked


@_UNIX_FLOCK_ONLY
@pytest.mark.asyncio
async def test_release_completes_despite_cancellation(tmp_path: Path, mocker: MockerFixture) -> None:
    lock = AsyncFileLock(str(tmp_path / "a"))
    await lock.acquire()
    # A slow unlock lets the cancellation land mid-release; shield must still drive it to completion.
    mocker.patch("filelock._unix.fcntl.flock", side_effect=lambda _fd, _op: time.sleep(0.2))
    task = asyncio.ensure_future(lock.release())
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not lock.is_locked
    assert lock.lock_counter == 0


@pytest.mark.asyncio
async def test_async_zero_write_rolls_back_acquire(tmp_path: Path, mocker: MockerFixture) -> None:
    mocker.patch("filelock._util.os.write", return_value=0)

    lock = AsyncSoftFileLock(str(tmp_path / "a"))
    with pytest.raises(OSError, match="0 bytes"):
        await lock.acquire()
    assert not lock.is_locked
