from __future__ import annotations

import asyncio
import gc
import logging
import os
import sys
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from errno import EINTR, EIO, ENOSYS
from pathlib import Path, PurePath
from typing import TYPE_CHECKING, Literal

import pytest

from filelock import (
    AsyncFileLock,
    AsyncSoftFileLock,
    BaseAsyncFileLock,
    CloseErrorPolicy,
    ContextErrorPolicy,
    Timeout,
)

if sys.version_info >= (3, 11):  # pragma: no cover (py311+)
    from builtins import BaseExceptionGroup, ExceptionGroup
else:  # pragma: no cover (<py311)
    from exceptiongroup import BaseExceptionGroup, ExceptionGroup

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

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


@pytest.mark.parametrize(
    "mode",
    [pytest.param("finite", id="finite"), pytest.param("nonblocking", id="nonblocking")],
)
@pytest.mark.asyncio
async def test_failed_acquire_keeps_holder_registered(tmp_path: Path, mode: Literal["finite", "nonblocking"]) -> None:
    lock_path = tmp_path / "test.lock"
    async with AsyncFileLock(lock_path, run_in_executor=False):
        with pytest.raises(Timeout):
            await _acquire_for_mode(AsyncFileLock(lock_path, run_in_executor=False), mode)
        with pytest.raises(RuntimeError, match="Deadlock"):
            await AsyncFileLock(lock_path, run_in_executor=False).acquire(cancel_check=lambda: True)


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


@pytest.mark.parametrize("lock_type", [AsyncFileLock, AsyncSoftFileLock])
@pytest.mark.asyncio
async def test_cross_task_blocking_acquire_queues(tmp_path: Path, lock_type: type[BaseAsyncFileLock]) -> None:
    # A blocking acquire from a different task must queue behind the holder, not raise. Polling yields the event
    # loop on every iteration, so the holder keeps running and releases; only a same-task reentry can deadlock.
    lock_path = tmp_path / "test.lock"
    events: list[str] = []

    async def holder() -> None:
        async with lock_type(lock_path):
            events.append("holder:acquired")
            await asyncio.sleep(0.05)  # the waiter starts polling while the lock is held
            events.append("holder:released")

    async def waiter() -> None:
        await asyncio.sleep(0)  # let the holder grab the lock first
        async with lock_type(lock_path):
            events.append("waiter:acquired")

    async with asyncio.TaskGroup() as tg:  # raises if any acquire false-positives as a deadlock
        tg.create_task(holder())
        tg.create_task(waiter())

    assert events.index("holder:acquired") < events.index("holder:released") < events.index("waiter:acquired")


@pytest.mark.parametrize("lock_type", [AsyncFileLock, AsyncSoftFileLock])
@pytest.mark.asyncio
async def test_release_from_different_task_clears_registry(tmp_path: Path, lock_type: type[BaseAsyncFileLock]) -> None:
    # The holder scope is pinned at commit time, so releasing from a different task still drops the registry entry.
    lock_path = tmp_path / "test.lock"
    lock = lock_type(lock_path)
    await lock.acquire()

    async def release() -> None:
        await lock.release()

    await asyncio.create_task(release())

    async with lock_type(lock_path):  # would raise Deadlock if the scoped entry had leaked
        pass


@pytest.mark.skipif(sys.platform == "win32", reason="unix-only symlink test")
@pytest.mark.parametrize("lock_type", [AsyncFileLock, AsyncSoftFileLock])
@pytest.mark.asyncio
async def test_symlink_same_canonical_path(tmp_path: Path, lock_type: type[BaseAsyncFileLock]) -> None:
    # A symlinked parent directory resolves to the same canonical key; the final component stays literal.
    real_dir = tmp_path / "real"
    real_dir.mkdir()
    (tmp_path / "link").symlink_to(real_dir)

    lock1 = lock_type(str(real_dir / "test.lock"))
    async with lock1:
        lock2 = lock_type(str(tmp_path / "link" / "test.lock"))
        with pytest.raises(RuntimeError, match="Deadlock"):
            await lock2.acquire()


@_UNIX_FLOCK_ONLY
@pytest.mark.parametrize(
    ("depth", "force"),
    [
        pytest.param(1, False, id="direct"),
        pytest.param(2, False, id="nested"),
        pytest.param(2, True, id="forced"),
    ],
)
@pytest.mark.asyncio
async def test_release_drops_acquisition_key_after_parent_retarget(tmp_path: Path, depth: int, force: bool) -> None:
    lock_path, original_path, replacement_path = _symlinked_lock_paths(tmp_path)
    lock = AsyncFileLock(lock_path, run_in_executor=False)
    for _depth in range(depth):
        await lock.acquire()
    _retarget_parent(lock_path, replacement_path)

    if force:
        await lock.release(force=True)
    else:
        for _depth in range(depth):
            await lock.release()

    async with AsyncFileLock(original_path, run_in_executor=False) as successor:
        assert successor.is_locked


@_UNIX_FLOCK_ONLY
@pytest.mark.asyncio
async def test_release_keeps_retargeted_parent_holder_registered(tmp_path: Path) -> None:
    lock_path, _original_path, replacement_path = _symlinked_lock_paths(tmp_path)
    original = AsyncFileLock(lock_path, run_in_executor=False)
    await original.acquire()
    _retarget_parent(lock_path, replacement_path)
    async with AsyncFileLock(replacement_path, run_in_executor=False):
        await original.release()
        with pytest.raises(RuntimeError, match="Deadlock"):
            await AsyncFileLock(replacement_path, run_in_executor=False).acquire(cancel_check=lambda: True)


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


def _symlinked_lock_paths(tmp_path: Path) -> tuple[Path, Path, Path]:
    original = tmp_path / "original"
    replacement = tmp_path / "replacement"
    original.mkdir()
    replacement.mkdir()
    link = tmp_path / "link"
    link.symlink_to(original, target_is_directory=True)
    return link / "test.lock", original / "test.lock", replacement / "test.lock"


def _retarget_parent(lock_path: Path, replacement_path: Path) -> None:
    link = lock_path.parent
    link.unlink()
    link.symlink_to(replacement_path.parent, target_is_directory=True)


async def _acquire_for_mode(lock: BaseAsyncFileLock, mode: Literal["finite", "nonblocking"]) -> None:
    if mode == "finite":
        await lock.acquire(timeout=0)
    else:
        await lock.acquire(blocking=False)


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


@pytest.mark.asyncio
async def test_async_zero_write_rolls_back_acquire(tmp_path: Path, mocker: MockerFixture) -> None:
    mocker.patch("filelock._util.os.write", return_value=0)

    lock = AsyncSoftFileLock(str(tmp_path / "a"))
    with pytest.raises(OSError, match="0 bytes"):
        await lock.acquire()
    assert not lock.is_locked


@pytest.mark.parametrize(
    ("policy", "surfaces"),
    [pytest.param("default", True, id="default"), pytest.param("suppress", False, id="suppress")],
)
@pytest.mark.asyncio
async def test_soft_close_error_policy_cleans_marker(
    tmp_path: Path,
    mocker: MockerFixture,
    policy: CloseErrorPolicy,
    *,
    surfaces: bool,
) -> None:
    lock_path = tmp_path / "a"
    lock = AsyncSoftFileLock(lock_path, close_error_policy=policy, run_in_executor=False)
    await lock.acquire()
    with _close_after_commit(mocker) as (close_error, attempts):
        if surfaces:
            with pytest.raises(OSError, match="close failed") as info:
                await lock.release()
            assert info.value is close_error
        else:
            await lock.release()
    assert (len(attempts), lock.is_locked, lock.lock_counter, lock_path.exists()) == (1, False, 0, False)


@pytest.mark.parametrize(
    ("depth", "force"),
    [pytest.param(2, False, id="nested"), pytest.param(2, True, id="forced")],
)
@pytest.mark.asyncio
async def test_soft_close_error_suppression_releases_once(
    tmp_path: Path,
    mocker: MockerFixture,
    depth: int,
    *,
    force: bool,
) -> None:
    lock_path = tmp_path / "a"
    lock = AsyncSoftFileLock(lock_path, close_error_policy="suppress", run_in_executor=False)
    for _acquisition in range(depth):
        await lock.acquire()
    with _close_after_commit(mocker) as (_, attempts):
        if force:
            await lock.release(force=True)
        else:
            for _release in range(depth):
                await lock.release()
    assert (len(attempts), lock.is_locked, lock.lock_counter, lock_path.exists()) == (1, False, 0, False)


@pytest.mark.parametrize(
    "use_proxy",
    [pytest.param(False, id="direct"), pytest.param(True, id="proxy")],
)
@pytest.mark.asyncio
async def test_soft_context_surfaces_close_error_after_cleanup(
    tmp_path: Path,
    mocker: MockerFixture,
    *,
    use_proxy: bool,
) -> None:
    lock_path = tmp_path / "a"
    lock = AsyncSoftFileLock(lock_path, run_in_executor=False)
    with _close_after_commit(mocker) as (close_error, attempts), pytest.raises(OSError, match="close failed") as info:
        async with await lock.acquire() if use_proxy else lock:
            pass
    assert (info.value, len(attempts), lock.is_locked, lock_path.exists()) == (close_error, 1, False, False)


@pytest.mark.asyncio
async def test_soft_second_release_does_not_close_reused_descriptor(
    tmp_path: Path,
    mocker: MockerFixture,
) -> None:
    lock = AsyncSoftFileLock(tmp_path / "a", run_in_executor=False)
    await lock.acquire()
    with _close_after_commit(mocker) as (close_error, attempts), pytest.raises(OSError, match="close failed") as info:
        await lock.release()
    assert info.value is close_error
    reused_fd = os.open(tmp_path / "reused", os.O_CREAT | os.O_WRONLY)
    assert reused_fd == attempts[0]
    try:
        await lock.release()
        os.fstat(reused_fd)
    finally:
        os.close(reused_fd)


@contextmanager
def _close_after_commit(mocker: MockerFixture) -> Iterator[tuple[OSError, list[int]]]:
    real_close = os.close
    close_error = OSError(EINTR, "close failed")
    attempts: list[int] = []

    def close(fd: int) -> None:
        real_close(fd)
        attempts.append(fd)
        raise close_error

    close_mock = mocker.patch("filelock._soft.os.close", side_effect=close)
    try:
        yield close_error, attempts
    finally:
        mocker.stop(close_mock)


@pytest.mark.parametrize(
    "use_proxy",
    [pytest.param(False, id="direct"), pytest.param(True, id="proxy")],
)
@pytest.mark.asyncio
async def test_context_group_detaches_release_context(
    tmp_path: Path,
    close_failure: tuple[Callable[[int], None], OSError, RuntimeError],
    *,
    use_proxy: bool,
) -> None:
    capture, release_error, release_cause = close_failure
    body_error = ValueError("body failed")
    body_cause = LookupError("body cause")
    lock = AsyncFileLock(
        str(tmp_path / "a"),
        thread_local=False,
        context_error_policy="group",
        close_error_policy="raise",
        on_acquired=capture,
    )
    with pytest.raises(ExceptionGroup) as info:
        async with await lock.acquire() if use_proxy else lock:
            raise body_error from body_cause
    assert (
        info.value.exceptions,
        body_error.__context__,
        release_error.__context__,
        body_error.__cause__,
        release_error.__cause__,
        body_error.__traceback__ is not None,
        release_error.__traceback__ is not None,
    ) == ((body_error, release_error), None, None, body_cause, release_cause, True, True)


@pytest.mark.skipif(sys.version_info < (3, 11), reason="standard exception-group rendering requires Python 3.11")
@pytest.mark.parametrize(
    "use_proxy",
    [pytest.param(False, id="direct"), pytest.param(True, id="proxy")],
)
@pytest.mark.asyncio
async def test_context_group_renders_independent_leaves(
    tmp_path: Path,
    close_failure: tuple[Callable[[int], None], OSError, RuntimeError],
    *,
    use_proxy: bool,
) -> None:
    capture, release_error, _ = close_failure
    release_error.__cause__ = None
    release_error.__suppress_context__ = False
    body_error = ValueError("body failed")
    lock = AsyncFileLock(
        str(tmp_path / "a"),
        thread_local=False,
        context_error_policy="group",
        close_error_policy="raise",
        on_acquired=capture,
    )
    with pytest.raises(ExceptionGroup) as info:
        async with await lock.acquire() if use_proxy else lock:
            raise body_error
    group_rendering = "".join(traceback.format_exception(info.value))
    release_rendering = "".join(traceback.format_exception(release_error))
    assert (
        group_rendering.count("ValueError: body failed"),
        release_rendering.count("ValueError: body failed"),
        release_rendering.count("RuntimeError: release cause"),
        release_rendering.count("OSError: release failed"),
    ) == (1, 0, 0, 1)


@pytest.mark.parametrize(
    "use_proxy",
    [pytest.param(False, id="direct"), pytest.param(True, id="proxy")],
)
@pytest.mark.asyncio
async def test_context_chain_keeps_release_error_with_body_in_context(
    tmp_path: Path,
    close_failure: tuple[Callable[[int], None], OSError, RuntimeError],
    *,
    use_proxy: bool,
) -> None:
    capture, release_error, _ = close_failure
    body_error = ValueError("body failed")
    lock = AsyncFileLock(
        str(tmp_path / "a"),
        thread_local=False,
        context_error_policy="chain",
        close_error_policy="raise",
        on_acquired=capture,
    )
    with pytest.raises(OSError, match="release failed") as info:
        async with await lock.acquire() if use_proxy else lock:
            raise body_error
    assert (info.value, release_error.__context__) == (release_error, body_error)


@pytest.mark.parametrize(
    "policy",
    [pytest.param("chain", id="chain"), pytest.param("group", id="group")],
)
@pytest.mark.parametrize(
    "use_proxy",
    [pytest.param(False, id="direct"), pytest.param(True, id="proxy")],
)
@pytest.mark.asyncio
async def test_context_body_only_failure_propagates_body(
    tmp_path: Path, policy: ContextErrorPolicy, *, use_proxy: bool
) -> None:
    lock = AsyncSoftFileLock(str(tmp_path / "a"), context_error_policy=policy)
    body = ValueError("body failed")
    with pytest.raises(ValueError, match="body failed") as info:
        async with await lock.acquire() if use_proxy else lock:
            raise body
    assert info.value is body


@pytest.mark.parametrize(
    "policy",
    [pytest.param("chain", id="chain"), pytest.param("group", id="group")],
)
@pytest.mark.parametrize(
    "use_proxy",
    [pytest.param(False, id="direct"), pytest.param(True, id="proxy")],
)
@pytest.mark.asyncio
async def test_context_release_only_failure_propagates_release(
    tmp_path: Path,
    close_failure: tuple[Callable[[int], None], OSError, RuntimeError],
    policy: ContextErrorPolicy,
    *,
    use_proxy: bool,
) -> None:
    capture, release_error, release_cause = close_failure
    lock = AsyncFileLock(
        str(tmp_path / "a"),
        thread_local=False,
        context_error_policy=policy,
        close_error_policy="raise",
        on_acquired=capture,
    )
    with pytest.raises(OSError, match="release failed") as info:
        async with await lock.acquire() if use_proxy else lock:
            pass
    assert (info.value, release_error.__context__, release_error.__cause__) == (release_error, None, release_cause)


@pytest.mark.asyncio
async def test_context_group_base_exception_leaf_is_base_group(
    tmp_path: Path, close_failure: tuple[Callable[[int], None], OSError, RuntimeError]
) -> None:
    capture, _, _ = close_failure
    lock = AsyncFileLock(
        str(tmp_path / "a"),
        thread_local=False,
        context_error_policy="group",
        close_error_policy="raise",
        on_acquired=capture,
    )
    with pytest.raises(BaseExceptionGroup) as info:
        async with lock:
            raise KeyboardInterrupt
    assert not isinstance(info.value, ExceptionGroup)
    assert [type(leaf) for leaf in info.value.exceptions] == [KeyboardInterrupt, OSError]


@pytest.mark.asyncio
async def test_context_group_preserves_user_release_cancellation_group(tmp_path: Path) -> None:
    body_error = ValueError("body failed")
    backend_cancellation = asyncio.CancelledError("backend canceled")
    backend_error = OSError(EIO, "backend failed")
    release_group = BaseExceptionGroup(
        "lock release cancellation and backend release both failed",
        (backend_cancellation, backend_error),
    )
    fail_release = True

    class GroupReleaseLock(BaseAsyncFileLock):
        def _acquire(self) -> None:
            self._context.lock_file_fd = 1

        def _release(self) -> None:
            nonlocal fail_release
            if fail_release:
                fail_release = False
                raise release_group
            self._context.lock_file_fd = None

    lock = GroupReleaseLock(tmp_path / "a", run_in_executor=False, context_error_policy="group")
    with pytest.raises(BaseExceptionGroup) as info:
        async with lock:
            raise body_error

    assert info.value.exceptions == (body_error, release_group)
    assert info.value.exceptions[1] is release_group
    assert release_group.exceptions == (backend_cancellation, backend_error)
    await lock.release()


def _fail_close_of(mocker: MockerFixture, lock: BaseAsyncFileLock, error: OSError) -> None:
    # Fail os.close only for this lock's descriptor, so an unrelated lock's __del__ close during the test is untouched.
    fd = lock._context.lock_file_fd
    real_close = os.close

    def close(target: int) -> None:
        if target == fd:
            raise error
        real_close(target)

    mocker.patch("filelock._api.os.close", side_effect=close)


@_UNIX_FLOCK_ONLY
@pytest.mark.asyncio
async def test_close_error_raise_propagates(tmp_path: Path, mocker: MockerFixture) -> None:
    lock = AsyncFileLock(str(tmp_path / "a"), close_error_policy="raise")
    await lock.acquire()
    _fail_close_of(mocker, lock, OSError(EIO, "close failed"))
    with pytest.raises(OSError, match="close failed"):
        await lock.release()
    assert not lock.is_locked


@_UNIX_FLOCK_ONLY
@pytest.mark.asyncio
async def test_close_error_default_suppressed_on_unix(tmp_path: Path, mocker: MockerFixture) -> None:
    lock = AsyncFileLock(str(tmp_path / "a"))  # default policy
    await lock.acquire()
    _fail_close_of(mocker, lock, OSError(EIO, "close failed"))
    await lock.release()
    assert not lock.is_locked


@_UNIX_FLOCK_ONLY
@pytest.mark.asyncio
async def test_fallback_to_soft_disabled_raises_enosys(tmp_path: Path, mocker: MockerFixture) -> None:
    mocker.patch("filelock._unix.fcntl.flock", side_effect=OSError(ENOSYS, "no flock"))
    lock = AsyncFileLock(str(tmp_path / "a"), fallback_to_soft=False)
    with pytest.raises(OSError, match="no flock"):
        await lock.acquire()
    assert not lock.is_locked
    assert type(lock).__name__ == "AsyncUnixFileLock"  # not swapped to the soft class


def test_preserve_lock_file_async_soft_rejects(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="preserve_lock_file"):
        AsyncSoftFileLock(str(tmp_path / "a"), preserve_lock_file=True)


@_UNIX_FLOCK_ONLY
@pytest.mark.asyncio
async def test_preserve_lock_file_async_release_keeps_pathname(tmp_path: Path) -> None:
    path = tmp_path / "a"
    lock = AsyncFileLock(str(path), preserve_lock_file=True)
    await lock.acquire()
    await lock.release()
    assert path.exists()  # the native pathname survives an async release
    assert type(lock).__name__ == "AsyncUnixFileLock"


def _failing_on_acquired(_fd: int) -> None:
    msg = "hook failed"
    raise RuntimeError(msg)


def test_on_acquired_async_soft_rejects(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="on_acquired"):
        AsyncSoftFileLock(str(tmp_path / "a"), on_acquired=lambda _fd: None)


@_UNIX_FLOCK_ONLY
@pytest.mark.asyncio
async def test_on_acquired_runs_in_backend_executor(tmp_path: Path) -> None:
    hook_thread = -1
    fd_while_held = -1

    def hook(fd: int) -> None:
        nonlocal hook_thread, fd_while_held
        hook_thread = threading.get_ident()
        if lock.is_locked:
            fd_while_held = fd

    lock = AsyncFileLock(str(tmp_path / "a"), thread_local=False, on_acquired=hook)
    await lock.acquire()
    try:
        assert fd_while_held >= 0  # the hook ran while the lock was held, with a real descriptor
        assert hook_thread != threading.get_ident()  # in the backend executor, not the event-loop thread
    finally:
        await lock.release()


@_UNIX_FLOCK_ONLY
@pytest.mark.asyncio
async def test_on_acquired_async_failure_releases(tmp_path: Path) -> None:
    lock = AsyncFileLock(str(tmp_path / "a"), thread_local=False, on_acquired=_failing_on_acquired)
    with pytest.raises(RuntimeError, match="hook failed"):
        await lock.acquire()
    assert not lock.is_locked
    assert lock.lock_counter == 0


@pytest.mark.asyncio
async def test_on_acquired_rollback_group_detaches_release_context(
    tmp_path: Path, close_failure: tuple[Callable[[int], None], OSError, RuntimeError]
) -> None:
    capture, release_error, release_cause = close_failure
    callback_error = RuntimeError("hook failed")
    callback_cause = LookupError("hook cause")

    def fail(fd: int) -> None:
        capture(fd)
        raise callback_error from callback_cause

    lock = AsyncFileLock(str(tmp_path / "a"), thread_local=False, close_error_policy="raise", on_acquired=fail)
    with pytest.raises(ExceptionGroup) as info:
        await lock.acquire()
    assert (
        info.value.exceptions,
        callback_error.__context__,
        release_error.__context__,
        callback_error.__cause__,
        release_error.__cause__,
        callback_error.__traceback__ is not None,
        release_error.__traceback__ is not None,
    ) == ((callback_error, release_error), None, None, callback_cause, release_cause, True, True)
