from __future__ import annotations

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from errno import EIO
from typing import TYPE_CHECKING

import pytest

from filelock import (
    AsyncFileLock,
    BaseAsyncFileLock,
    ContextErrorPolicy,
)
from tests.async_lock.cancellation import (
    FailingSubmitExecutor,
    assert_cancellation_message,
    assert_file_lock_state,
)
from tests.capability_marks import NEEDS_FCNTL, XFAIL_WITHOUT_COROUTINE_CANCELLATION

if TYPE_CHECKING:
    from pathlib import Path

    from pytest_mock import MockerFixture


@pytest.mark.asyncio
async def test_backend_cancellation_after_acquire_rolls_back(tmp_path: Path) -> None:
    backend_cancellation = asyncio.CancelledError("backend canceled")
    released = False

    class CancellingFileLock(BaseAsyncFileLock):
        async def _acquire(self) -> None:  # ty: ignore[incompatible-method-override]  # coroutine backends are supported
            self._context.lock_file_fd = 1
            raise backend_cancellation

        async def _release(self) -> None:  # ty: ignore[incompatible-method-override]  # coroutine backends are supported
            nonlocal released
            released = True
            self._context.lock_file_fd = None

    lock = CancellingFileLock(tmp_path / "a", run_in_executor=False)
    with pytest.raises(asyncio.CancelledError, match="backend canceled"):
        await lock.acquire()
    assert (released, lock.is_locked, lock.lock_counter) == (True, False, 0)


@pytest.mark.asyncio
@XFAIL_WITHOUT_COROUTINE_CANCELLATION
async def test_backend_cancellation_rollback_failure_chains(tmp_path: Path) -> None:
    backend_cancellation = asyncio.CancelledError("backend canceled")
    rollback_error = OSError(EIO, "rollback failed")
    prior_rollback_error = _prior_rollback_failures()
    lock = _lock_failing_rollback_once(tmp_path, "chain", backend_cancellation, rollback_error, prior_rollback_error)
    with pytest.raises(OSError, match="rollback failed") as info:
        await lock.acquire()
    assert (info.value, rollback_error.__context__, backend_cancellation.__context__) == (
        rollback_error,
        backend_cancellation,
        prior_rollback_error,
    )
    assert (lock.is_locked, lock.lock_counter) == (True, 1)

    await lock.release()
    assert (lock.is_locked, lock.lock_counter) == (False, 0)


def _prior_rollback_failures() -> ExceptionGroup[LookupError]:
    prior_cause = RuntimeError("prior rollback cause")
    prior_rollback_error = ExceptionGroup("prior rollback failures", (LookupError("prior rollback failure"),))
    prior_rollback_error.__cause__ = prior_cause
    prior_cause.__context__ = prior_rollback_error
    return prior_rollback_error


def _lock_failing_rollback_once(
    tmp_path: Path,
    policy: ContextErrorPolicy,
    backend_cancellation: asyncio.CancelledError,
    rollback_error: OSError,
    prior_rollback_error: ExceptionGroup[LookupError],
) -> BaseAsyncFileLock:
    fail_rollback = True

    class CancellingFileLock(BaseAsyncFileLock):
        async def _acquire(self) -> None:  # ty: ignore[incompatible-method-override]  # coroutine backends are supported
            self._context.lock_file_fd = 1
            raise backend_cancellation

        async def _release(self) -> None:  # ty: ignore[incompatible-method-override]  # coroutine backends are supported
            nonlocal fail_rollback
            if fail_rollback:
                fail_rollback = False
                try:
                    raise prior_rollback_error
                except ExceptionGroup:
                    raise rollback_error  # ruff:ignore[raise-without-from-inside-except]  # exercise implicit backend context preservation
            self._context.lock_file_fd = None

    return CancellingFileLock(tmp_path / "a", run_in_executor=False, context_error_policy=policy)


@pytest.mark.asyncio
@XFAIL_WITHOUT_COROUTINE_CANCELLATION
async def test_backend_cancellation_rollback_failure_groups(tmp_path: Path) -> None:
    backend_cancellation = asyncio.CancelledError("backend canceled")
    rollback_error = OSError(EIO, "rollback failed")
    prior_rollback_error = _prior_rollback_failures()
    lock = _lock_failing_rollback_once(tmp_path, "group", backend_cancellation, rollback_error, prior_rollback_error)
    with pytest.raises(BaseExceptionGroup) as info:
        await lock.acquire()
    assert (info.value.exceptions, rollback_error.__context__) == (
        (backend_cancellation, rollback_error),
        prior_rollback_error,
    )
    assert (lock.is_locked, lock.lock_counter) == (True, 1)

    await lock.release()
    assert (lock.is_locked, lock.lock_counter) == (False, 0)


@pytest.mark.asyncio
async def test_caller_and_backend_cancellation_both_surface(tmp_path: Path) -> None:
    acquire_started = asyncio.Event()
    finish_acquire = asyncio.Event()
    backend_cancellation = asyncio.CancelledError("backend canceled")

    class CancellingFileLock(BaseAsyncFileLock):
        async def _acquire(self) -> None:  # ty: ignore[incompatible-method-override]  # coroutine backends are supported
            self._context.lock_file_fd = 1
            acquire_started.set()
            await finish_acquire.wait()
            raise backend_cancellation

        async def _release(self) -> None:  # ty: ignore[incompatible-method-override]  # coroutine backends are supported
            self._context.lock_file_fd = None

    lock = CancellingFileLock(tmp_path / "a", run_in_executor=False, context_error_policy="group")
    task = asyncio.create_task(lock.acquire())
    await acquire_started.wait()
    task.cancel("caller canceled")
    finish_acquire.set()
    with pytest.raises(BaseExceptionGroup) as info:
        await task

    caller_cancellation, grouped_backend_cancellation = info.value.exceptions
    assert isinstance(caller_cancellation, asyncio.CancelledError)
    assert (caller_cancellation.args, grouped_backend_cancellation) == (("caller canceled",), backend_cancellation)
    assert (lock.is_locked, lock.lock_counter) == (False, 0)


@pytest.mark.asyncio
async def test_acquire_cancellation_before_executor_start_rolls_back(tmp_path: Path) -> None:
    executor_started = threading.Event()
    release_executor = threading.Event()
    acquired = threading.Event()
    with ThreadPoolExecutor(max_workers=1) as executor:
        blocker = executor.submit(_block_executor, executor_started, release_executor)
        assert executor_started.wait(timeout=5)
        lock = AsyncFileLock(tmp_path / "a", executor=executor, on_acquired=lambda _fd: acquired.set())
        task = asyncio.create_task(lock.acquire())
        await asyncio.sleep(0)
        # The acquire resolves the lock path on the executor first, then creates the directory there, so queue a second
        # blocker behind the first job and a third behind the directory job to hold the backend attempt after them.
        second_started = threading.Event()
        release_second = threading.Event()
        second_blocker = executor.submit(_block_executor, second_started, release_second)
        release_executor.set()
        assert await asyncio.to_thread(second_started.wait, 5)
        # The resolved path wakes the task, which queues the directory job behind the second blocker.
        await asyncio.sleep(0.05)  # simplint: ignore[sleep-in-test]  # needs an executor-queue signal
        third_started = threading.Event()
        release_third = threading.Event()
        third_blocker = executor.submit(_block_executor, third_started, release_third)
        release_second.set()
        assert await asyncio.to_thread(third_started.wait, 5)
        # The created directory wakes the task, which queues the attempt behind the third blocker.
        await asyncio.sleep(0.05)  # simplint: ignore[sleep-in-test]  # needs an executor-queue signal
        assert (lock.lock_counter, acquired.is_set()) == (1, False)
        task.cancel()
        release_third.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        blocker.result(timeout=5)
        second_blocker.result(timeout=5)
        third_blocker.result(timeout=5)

    assert acquired.is_set()
    assert (lock.is_locked, lock.lock_counter) == (False, 0)
    assert_file_lock_state(str(tmp_path / "a"), available=True)


def _block_executor(executor_started: threading.Event, release_executor: threading.Event) -> None:
    executor_started.set()
    assert release_executor.wait(timeout=5)


@pytest.mark.asyncio
async def test_executor_shutdown_drops_queued_acquire_with_runtime_error(tmp_path: Path) -> None:
    executor = ThreadPoolExecutor(max_workers=1)
    lock = AsyncFileLock(tmp_path / "a", executor=executor)
    executor_started = threading.Event()
    release_executor = threading.Event()
    blocker = executor.submit(_block_executor, executor_started, release_executor)
    assert executor_started.wait(timeout=5)
    task = asyncio.create_task(lock.acquire())
    await asyncio.sleep(0)
    executor.shutdown(wait=False, cancel_futures=True)
    release_executor.set()
    blocker.result(timeout=5)

    with pytest.raises(RuntimeError, match="executor shut down before running"):
        await task
    assert (lock.is_locked, lock.lock_counter) == (False, 0)
    assert_file_lock_state(str(tmp_path / "a"), available=True)
    lock.executor = None
    await lock.release()
    assert_file_lock_state(str(tmp_path / "a"), available=True)


@pytest.mark.asyncio
async def test_executor_shutdown_drops_queued_release_with_runtime_error(tmp_path: Path) -> None:
    executor = ThreadPoolExecutor(max_workers=1)
    lock = AsyncFileLock(tmp_path / "a", executor=executor)
    await lock.acquire()
    executor_started = threading.Event()
    release_executor = threading.Event()
    blocker = executor.submit(_block_executor, executor_started, release_executor)
    assert executor_started.wait(timeout=5)
    task = asyncio.create_task(lock.release())
    await asyncio.sleep(0)
    executor.shutdown(wait=False, cancel_futures=True)
    release_executor.set()
    blocker.result(timeout=5)

    with pytest.raises(RuntimeError, match="executor shut down before running"):
        await task
    assert (lock.is_locked, lock.lock_counter) == (True, 1)
    assert_file_lock_state(str(tmp_path / "a"), available=False)
    lock.executor = None
    await lock.release()
    assert_file_lock_state(str(tmp_path / "a"), available=True)


@pytest.mark.asyncio
async def test_release_whose_submission_failed_never_runs(tmp_path: Path) -> None:
    with FailingSubmitExecutor(after_call_ran=False) as executor:
        lock = AsyncFileLock(tmp_path / "a", executor=executor)
        await lock.acquire()
        executor_started = threading.Event()
        release_executor = threading.Event()
        blocker = executor.submit(_block_executor, executor_started, release_executor)
        assert executor_started.wait(timeout=5)
        executor.fail_next_submit = True

        with pytest.raises(RuntimeError, match="can't start new thread"):
            await lock.release()
        release_executor.set()
        blocker.result(timeout=5)
        # The pool has one worker, so it took the call queued behind the blocker before it ran this one.
        executor.submit(threading.get_ident).result(timeout=5)

        assert (lock.is_locked, lock.lock_counter) == (True, 1)
        assert_file_lock_state(str(tmp_path / "a"), available=False)
        await lock.release()
        assert_file_lock_state(str(tmp_path / "a"), available=True)


@pytest.mark.asyncio
async def test_release_that_ran_before_its_submission_failed_counts(tmp_path: Path) -> None:
    with FailingSubmitExecutor(after_call_ran=True) as executor:
        lock = AsyncFileLock(tmp_path / "a", executor=executor)
        await lock.acquire()
        executor.fail_next_submit = True

        await lock.release()

        assert (lock.is_locked, lock.lock_counter) == (False, 0)
        assert_file_lock_state(str(tmp_path / "a"), available=True)


@NEEDS_FCNTL
@pytest.mark.asyncio
async def test_acquire_cancellation_does_not_release_later_claim(tmp_path: Path) -> None:  # pragma: needs fcntl
    hook_started = asyncio.Event()
    finish_hook = threading.Event()
    loop = asyncio.get_running_loop()

    def block_hook(_fd: int) -> None:
        loop.call_soon_threadsafe(hook_started.set)
        assert finish_hook.wait(timeout=5)

    lock = AsyncFileLock(tmp_path / "a", on_acquired=block_hook)
    first_acquire = asyncio.create_task(lock.acquire())
    await hook_started.wait()
    second_acquire = asyncio.create_task(lock.acquire())
    first_acquire.cancel("cancel first acquire")
    finish_hook.set()
    with pytest.raises(asyncio.CancelledError) as info:
        await first_acquire
    assert_cancellation_message(info.value, "cancel first acquire")
    await second_acquire

    assert (lock.is_locked, lock.lock_counter) == (True, 1)
    assert_file_lock_state(str(tmp_path / "a"), available=False)
    await lock.release()
    assert_file_lock_state(str(tmp_path / "a"), available=True)


@NEEDS_FCNTL
@pytest.mark.asyncio
@XFAIL_WITHOUT_COROUTINE_CANCELLATION
async def test_cancelled_queued_acquire_does_not_claim_transition(tmp_path: Path) -> None:  # pragma: needs fcntl
    hook_started = asyncio.Event()
    finish_hook = threading.Event()
    loop = asyncio.get_running_loop()

    def block_hook(_fd: int) -> None:
        loop.call_soon_threadsafe(hook_started.set)
        assert finish_hook.wait(timeout=5)

    lock = AsyncFileLock(tmp_path / "a", on_acquired=block_hook)
    first_acquire = asyncio.create_task(lock.acquire())
    await hook_started.wait()
    queued_acquire = asyncio.create_task(lock.acquire())
    await asyncio.sleep(0)
    queued_acquire.cancel("cancel queued acquire")
    with pytest.raises(asyncio.CancelledError) as info:
        await queued_acquire
    assert_cancellation_message(info.value, "cancel queued acquire")
    assert lock.lock_counter == 1

    finish_hook.set()
    await first_acquire
    await lock.acquire()
    assert lock.lock_counter == 2
    await lock.release()
    await lock.release()
    assert_file_lock_state(str(tmp_path / "a"), available=True)


@NEEDS_FCNTL
@pytest.mark.asyncio  # pragma: needs fcntl
async def test_acquire_repeated_cancellation_waits_for_rollback(tmp_path: Path, mocker: MockerFixture) -> None:
    hook_started = asyncio.Event()
    finish_hook = threading.Event()
    rollback_started = asyncio.Event()
    finish_rollback = threading.Event()
    loop = asyncio.get_running_loop()

    def block_hook(_fd: int) -> None:
        loop.call_soon_threadsafe(hook_started.set)
        assert finish_hook.wait(timeout=5)

    def block_rollback(_fd: int, _operation: int) -> None:
        loop.call_soon_threadsafe(rollback_started.set)
        assert finish_rollback.wait(timeout=5)

    lock = AsyncFileLock(tmp_path / "a", on_acquired=block_hook)
    task = asyncio.create_task(lock.acquire())
    await hook_started.wait()
    mocker.patch("filelock._native._unix.fcntl.flock", side_effect=block_rollback, autospec=True)
    task.cancel("first cancellation")
    finish_hook.set()
    await rollback_started.wait()
    task.cancel("second cancellation")
    finish_rollback.set()
    with pytest.raises(asyncio.CancelledError) as info:
        await task

    assert_cancellation_message(info.value, "first cancellation")
    assert (lock.is_locked, lock.lock_counter) == (False, 0)
    assert_file_lock_state(str(tmp_path / "a"), available=True)


@pytest.mark.asyncio
async def test_non_executor_immediate_acquire_precedes_scheduled_callback(tmp_path: Path) -> None:
    callbacks: list[str] = []
    asyncio.get_running_loop().call_soon(callbacks.append, "callback")
    lock = AsyncFileLock(tmp_path / "a", run_in_executor=False)

    await lock.acquire()

    assert callbacks == []
    await lock.release()
    await asyncio.sleep(0)
    assert callbacks == ["callback"]
