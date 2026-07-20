from __future__ import annotations

import asyncio
import sys
from errno import EIO
from typing import TYPE_CHECKING

import pytest
from capability_marks import XFAIL_WITHOUT_COROUTINE_CANCELLATION

from filelock import AsyncFileLock, BaseAsyncFileLock

if sys.version_info >= (3, 11):  # pragma: no cover (py311+)
    from builtins import BaseExceptionGroup, ExceptionGroup  # pragma: >=3.11 cover
else:  # pragma: no cover (<py311)
    from exceptiongroup import BaseExceptionGroup, ExceptionGroup

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.asyncio
async def test_release_serialized_skips_when_peer_already_released(tmp_path: Path) -> None:
    release_started = asyncio.Event()
    finish_release = asyncio.Event()
    releases = 0

    class Lock(BaseAsyncFileLock):
        async def _acquire(self) -> None:  # ty: ignore[invalid-method-override]
            self._context.lock_file_fd = 1

        async def _release(self) -> None:  # ty: ignore[invalid-method-override]
            nonlocal releases
            releases += 1
            release_started.set()
            await finish_release.wait()
            self._context.lock_file_fd = None

    lock = Lock(tmp_path / "a", run_in_executor=False)
    await lock.acquire()
    first = asyncio.create_task(lock.release())
    await release_started.wait()
    second = asyncio.create_task(lock.release())
    await asyncio.sleep(0)  # let the queued release block on the transition gate before the holder unlocks
    finish_release.set()
    await first
    await second

    assert releases == 1  # the queued release found the lock already gone and did no backend work
    assert (lock.is_locked, lock.lock_counter) == (False, 0)


@pytest.mark.asyncio
@XFAIL_WITHOUT_COROUTINE_CANCELLATION
async def test_release_cancellation_surfaces_backend_error(tmp_path: Path) -> None:
    backend_error = OSError(EIO, "backend release failed")
    release_started = asyncio.Event()
    finish_release = asyncio.Event()
    fail_release = True

    class Lock(BaseAsyncFileLock):
        async def _acquire(self) -> None:  # ty: ignore[invalid-method-override]
            self._context.lock_file_fd = 1

        async def _release(self) -> None:  # ty: ignore[invalid-method-override]
            if fail_release:
                release_started.set()
                await finish_release.wait()
                raise backend_error
            self._context.lock_file_fd = None

    lock = Lock(tmp_path / "a", run_in_executor=False)
    await lock.acquire()
    task = asyncio.create_task(lock.release())
    await release_started.wait()
    task.cancel("cancel release")
    finish_release.set()

    with pytest.raises(OSError, match="backend release failed") as info:
        await task
    assert info.value is backend_error
    assert isinstance(info.value.__context__, asyncio.CancelledError)
    assert (lock.is_locked, lock.lock_counter) == (True, 1)  # backend failed before unlocking, so state is kept

    fail_release = False
    await lock.release()
    assert (lock.is_locked, lock.lock_counter) == (False, 0)


@pytest.mark.asyncio
@XFAIL_WITHOUT_COROUTINE_CANCELLATION
async def test_acquire_cancellation_rollback_failure_surfaces_backend_error(tmp_path: Path) -> None:
    rollback_error = OSError(EIO, "rollback release failed")
    acquire_started = asyncio.Event()
    finish_acquire = asyncio.Event()
    fail_release = True

    class Lock(BaseAsyncFileLock):
        async def _acquire(self) -> None:  # ty: ignore[invalid-method-override]
            acquire_started.set()
            await finish_acquire.wait()
            self._context.lock_file_fd = 1

        async def _release(self) -> None:  # ty: ignore[invalid-method-override]
            if fail_release:
                raise rollback_error
            self._context.lock_file_fd = None

    lock = Lock(tmp_path / "a", run_in_executor=False)
    task = asyncio.create_task(lock.acquire())
    await acquire_started.wait()
    task.cancel("cancel acquire")
    finish_acquire.set()  # let the backend finish acquiring, so cancellation must roll the acquired lock back

    with pytest.raises(OSError, match="rollback release failed") as info:
        await task
    assert info.value is rollback_error
    assert isinstance(info.value.__context__, asyncio.CancelledError)
    assert (lock.is_locked, lock.lock_counter) == (True, 1)  # rollback failed before unlocking, so state is kept

    fail_release = False
    await lock.release()
    assert (lock.is_locked, lock.lock_counter) == (False, 0)


@pytest.mark.asyncio
async def test_registration_failure_rolls_back_and_surfaces_cleanup(tmp_path: Path) -> None:
    registration_error = RuntimeError("registration failed")
    rollback_error = OSError(EIO, "rollback release failed")
    fail_release = True

    class Lock(BaseAsyncFileLock):
        async def _acquire(self) -> None:  # ty: ignore[invalid-method-override]
            self._context.lock_file_fd = 1

        async def _release(self) -> None:  # ty: ignore[invalid-method-override]
            if fail_release:
                raise rollback_error
            self._context.lock_file_fd = None

        def _register_context_descriptor(self) -> None:  # ruff:ignore[no-self-use]  # overrides a base instance method
            raise registration_error

    lock = Lock(tmp_path / "a", run_in_executor=False)
    with pytest.raises(ExceptionGroup) as info:
        await lock.acquire()
    assert info.value.message == "descriptor registration cleanup failed"
    assert info.value.exceptions == (registration_error, rollback_error)
    assert (lock.is_locked, lock.lock_counter) == (True, 1)  # rollback failed before unlocking, so state is kept

    fail_release = False
    await lock.release()
    assert (lock.is_locked, lock.lock_counter) == (False, 0)


@pytest.mark.parametrize(
    "rollback_fails",
    [pytest.param(True, id="rollback-fails"), pytest.param(False, id="rollback-succeeds")],
)
@pytest.mark.asyncio
async def test_acquire_failure_with_registration_failure_rolls_back(tmp_path: Path, *, rollback_fails: bool) -> None:
    acquisition_error = RuntimeError("acquire failed")
    registration_error = LookupError("registration failed")
    rollback_error = OSError(EIO, "rollback release failed")
    fail_release = rollback_fails

    class Lock(BaseAsyncFileLock):
        async def _acquire(self) -> None:  # ty: ignore[invalid-method-override]
            self._context.lock_file_fd = 1
            raise acquisition_error

        async def _release(self) -> None:  # ty: ignore[invalid-method-override]
            if fail_release:
                raise rollback_error
            self._context.lock_file_fd = None

        def _register_context_descriptor(self) -> None:  # ruff:ignore[no-self-use]  # overrides a base instance method
            raise registration_error

    lock = Lock(tmp_path / "a", run_in_executor=False)
    with pytest.raises(ExceptionGroup) as info:
        await lock.acquire()
    assert info.value.message == "lock acquisition cleanup failed"
    if rollback_fails:
        assert info.value.exceptions == (acquisition_error, registration_error, rollback_error)
        assert (lock.is_locked, lock.lock_counter) == (True, 1)  # rollback failed before unlocking, so state is kept
        fail_release = False
        await lock.release()
    else:
        assert info.value.exceptions == (acquisition_error, registration_error)
    assert (lock.is_locked, lock.lock_counter) == (False, 0)


@pytest.mark.asyncio
async def test_context_group_reconciles_release_cancellation_with_backend_error(tmp_path: Path) -> None:
    body_error = ValueError("body failed")
    backend_error = OSError(EIO, "backend release failed")
    release_started = asyncio.Event()
    finish_release = asyncio.Event()
    fail_release = True

    class Lock(BaseAsyncFileLock):
        async def _acquire(self) -> None:  # ty: ignore[invalid-method-override]
            self._context.lock_file_fd = 1

        async def _release(self) -> None:  # ty: ignore[invalid-method-override]
            if fail_release:
                release_started.set()
                await finish_release.wait()
                raise backend_error
            self._context.lock_file_fd = None

    lock = Lock(tmp_path / "a", run_in_executor=False, context_error_policy="group")

    async def run_context() -> None:
        async with lock:
            raise body_error

    task = asyncio.create_task(run_context())
    await release_started.wait()
    task.cancel("cancel release")
    finish_release.set()

    with pytest.raises(BaseExceptionGroup) as info:
        await task
    grouped_body, grouped_cancellation, grouped_backend = info.value.exceptions
    assert info.value.message == "context body, release cancellation, and backend release failed"
    assert (grouped_body, grouped_backend) == (body_error, backend_error)
    assert isinstance(grouped_cancellation, asyncio.CancelledError)
    assert (lock.is_locked, lock.lock_counter) == (True, 1)  # backend failed before unlocking, so state is kept

    fail_release = False
    await lock.release()
    assert (lock.is_locked, lock.lock_counter) == (False, 0)


def test_sync_context_exit_is_unsupported(tmp_path: Path) -> None:
    # __enter__ already raises, so the with-statement never reaches __exit__; call the protocol method directly.
    lock = AsyncFileLock(str(tmp_path / "a"))
    with pytest.raises(NotImplementedError, match=r"async with"):
        lock.__exit__(None, None, None)
