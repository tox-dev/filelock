from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from errno import ENOSYS
from typing import TYPE_CHECKING

import pytest

from filelock import (
    AsyncFileLock,
    AsyncSoftFileLock,
    BaseAsyncFileLock,
    Timeout,
    UnixFileLock,
)
from tests.capability_marks import (
    NEEDS_FCNTL,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from pytest_mock import MockerFixture


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
async def test_executor_can_be_reassigned(tmp_path: Path) -> None:
    lock = AsyncFileLock(tmp_path / "a")
    with ThreadPoolExecutor(max_workers=1) as executor:
        lock.executor = executor
        assert lock.executor is executor
        async with lock:
            assert lock.is_locked
    lock.executor = None
    assert lock.executor is None


@pytest.mark.asyncio
async def test_coroutine_function(tmp_path: Path) -> None:
    acquired = released = False

    class AioFileLock(BaseAsyncFileLock):
        async def _acquire(self) -> None:  # ty: ignore[incompatible-method-override]  # coroutine backends are supported
            nonlocal acquired
            acquired = True
            self._context.lock_file_fd = 1

        async def _release(self) -> None:  # ty: ignore[incompatible-method-override]  # coroutine backends are supported
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


@pytest.mark.asyncio
async def test_coroutine_acquire_without_holding_polls_and_skips_on_acquired(tmp_path: Path) -> None:
    attempts: list[str] = []

    class NeverHoldsLock(BaseAsyncFileLock):
        async def _acquire(self) -> None:  # ty: ignore[incompatible-method-override]  # coroutine backends are supported
            attempts.append(self.lock_file)  # returns without setting the fd, so is_locked stays False

        async def _release(self) -> None:  # ty: ignore[incompatible-method-override]  # coroutine backends are supported
            self._context.lock_file_fd = None  # pragma: no cover  # never held, so release never runs

    lock = NeverHoldsLock(str(tmp_path / "a"), timeout=0.1)
    with pytest.raises(Timeout):
        await lock.acquire()

    # The coroutine backend returned without acquiring, so the tracker skipped on_acquired and the poll loop retried.
    assert len(attempts) >= 2
    assert not lock.is_locked


@NEEDS_FCNTL
@pytest.mark.asyncio  # pragma: needs fcntl
async def test_fallback_to_soft_disabled_raises_enosys(tmp_path: Path, mocker: MockerFixture) -> None:
    mocker.patch("filelock._native._unix.fcntl.flock", side_effect=OSError(ENOSYS, "no flock"), autospec=True)
    lock = AsyncFileLock(str(tmp_path / "a"), fallback_to_soft=False)
    with pytest.raises(OSError, match="no flock"):
        await lock.acquire()
    assert not lock.is_locked
    assert type(lock).__name__ == "AsyncUnixFileLock"  # not swapped to the soft class


@NEEDS_FCNTL
@pytest.mark.asyncio  # pragma: needs fcntl
@pytest.mark.filterwarnings("ignore:flock not supported on this filesystem:UserWarning")
async def test_fallback_to_soft_reprobes_after_chdir(
    working_directories: tuple[Path, Path], mocker: MockerFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    first, second = working_directories
    unsupported_flock = mocker.patch(
        "filelock._native._unix.fcntl.flock", side_effect=OSError(ENOSYS, "no flock"), autospec=True
    )
    lock = AsyncFileLock("a", run_in_executor=False)
    async with lock:
        assert (first / "a").read_bytes()
    mocker.stop(unsupported_flock)
    monkeypatch.chdir(second)

    async with lock:
        with pytest.raises(Timeout):
            UnixFileLock(second / "a").acquire(timeout=0)


def test_preserve_lock_file_async_soft_rejects(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="preserve_lock_file"):
        AsyncSoftFileLock(str(tmp_path / "a"), preserve_lock_file=True)


@NEEDS_FCNTL
@pytest.mark.asyncio
async def test_preserve_lock_file_async_release_keeps_pathname(tmp_path: Path) -> None:  # pragma: needs fcntl
    path = tmp_path / "a"
    lock = AsyncFileLock(str(path), preserve_lock_file=True)
    await lock.acquire()
    await lock.release()
    assert path.exists()  # the native pathname survives an async release
    assert type(lock).__name__ == "AsyncUnixFileLock"


def test_on_acquired_async_soft_rejects(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="on_acquired"):
        AsyncSoftFileLock(str(tmp_path / "a"), on_acquired=lambda _fd: None)


@NEEDS_FCNTL
@pytest.mark.asyncio
async def test_on_acquired_runs_in_backend_executor(tmp_path: Path) -> None:  # pragma: needs fcntl
    hook_thread = -1
    fd_while_held = -1

    def hook(fd: int) -> None:
        nonlocal hook_thread, fd_while_held
        hook_thread = threading.get_ident()
        if lock.is_locked:  # pragma: no branch  # on_acquired only ever runs while the lock is held
            fd_while_held = fd

    lock = AsyncFileLock(str(tmp_path / "a"), thread_local=False, on_acquired=hook)
    await lock.acquire()
    try:
        assert fd_while_held >= 0  # the hook ran while the lock was held, with a real descriptor
        assert hook_thread != threading.get_ident()  # in the backend executor, not the event-loop thread
    finally:
        await lock.release()


@NEEDS_FCNTL
@pytest.mark.asyncio
async def test_on_acquired_async_failure_releases(tmp_path: Path) -> None:  # pragma: needs fcntl
    lock = AsyncFileLock(str(tmp_path / "a"), thread_local=False, on_acquired=_failing_on_acquired)
    with pytest.raises(RuntimeError, match="hook failed"):
        await lock.acquire()
    assert not lock.is_locked
    assert lock.lock_counter == 0


def _failing_on_acquired(_fd: int) -> None:  # pragma: needs fcntl
    msg = "hook failed"
    raise RuntimeError(msg)


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
