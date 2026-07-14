from __future__ import annotations

import asyncio
import logging
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from errno import EIO
from typing import TYPE_CHECKING, Final

import pytest
from async_filelock_cancellation_helpers import assert_cancellation_message, assert_file_lock_state

from filelock import (
    AsyncFileLock,
    BaseAsyncFileLock,
    ContextErrorPolicy,
)

if sys.version_info >= (3, 11):  # pragma: no cover (py311+)
    from builtins import BaseExceptionGroup, ExceptionGroup
else:  # pragma: no cover (<py311)
    from exceptiongroup import BaseExceptionGroup, ExceptionGroup

if TYPE_CHECKING:
    from pathlib import Path

    from pytest_mock import MockerFixture

_UNIX_FLOCK_ONLY: Final[pytest.MarkDecorator] = pytest.mark.skipif(
    sys.platform == "win32", reason="native flock semantics are Unix-only"
)


@pytest.mark.asyncio
async def test_backend_cancellation_after_acquire_rolls_back(tmp_path: Path) -> None:
    backend_cancellation = asyncio.CancelledError("backend canceled")
    released = False

    class CancellingFileLock(BaseAsyncFileLock):
        async def _acquire(self) -> None:  # ty: ignore[invalid-method-override]
            self._context.lock_file_fd = 1
            raise backend_cancellation

        async def _release(self) -> None:  # ty: ignore[invalid-method-override]
            nonlocal released
            released = True
            self._context.lock_file_fd = None

    lock = CancellingFileLock(tmp_path / "a", run_in_executor=False)
    with pytest.raises(asyncio.CancelledError, match="backend canceled"):
        await lock.acquire()
    assert (released, lock.is_locked, lock.lock_counter) == (True, False, 0)


@pytest.mark.parametrize("policy", [pytest.param("chain", id="chain"), pytest.param("group", id="group")])
@pytest.mark.asyncio
async def test_backend_cancellation_rollback_failure_follows_policy(tmp_path: Path, policy: ContextErrorPolicy) -> None:
    backend_cancellation = asyncio.CancelledError("backend canceled")
    rollback_error = OSError(EIO, "rollback failed")
    prior_leaf = LookupError("prior rollback failure")
    prior_cause = RuntimeError("prior rollback cause")
    prior_rollback_error = ExceptionGroup("prior rollback failures", (prior_leaf,))
    prior_rollback_error.__cause__ = prior_cause
    prior_cause.__context__ = prior_rollback_error
    fail_rollback = True

    class CancellingFileLock(BaseAsyncFileLock):
        async def _acquire(self) -> None:  # ty: ignore[invalid-method-override]
            self._context.lock_file_fd = 1
            raise backend_cancellation

        async def _release(self) -> None:  # ty: ignore[invalid-method-override]
            if fail_rollback:
                try:
                    raise prior_rollback_error
                except ExceptionGroup:
                    raise rollback_error  # noqa: B904  # exercise implicit backend context preservation
            self._context.lock_file_fd = None

    lock = CancellingFileLock(tmp_path / "a", run_in_executor=False, context_error_policy=policy)
    if policy == "chain":
        with pytest.raises(OSError, match="rollback failed") as info:
            await lock.acquire()
        assert (info.value, rollback_error.__context__, backend_cancellation.__context__) == (
            rollback_error,
            backend_cancellation,
            prior_rollback_error,
        )
    else:
        with pytest.raises(BaseExceptionGroup) as info:
            await lock.acquire()
        assert (info.value.exceptions, rollback_error.__context__) == (
            (backend_cancellation, rollback_error),
            prior_rollback_error,
        )
    assert (lock.is_locked, lock.lock_counter) == (True, 1)

    fail_rollback = False
    await lock.release()
    assert (lock.is_locked, lock.lock_counter) == (False, 0)


@pytest.mark.asyncio
async def test_caller_and_backend_cancellation_both_surface(tmp_path: Path) -> None:
    acquire_started = asyncio.Event()
    finish_acquire = asyncio.Event()
    backend_cancellation = asyncio.CancelledError("backend canceled")

    class CancellingFileLock(BaseAsyncFileLock):
        async def _acquire(self) -> None:  # ty: ignore[invalid-method-override]
            self._context.lock_file_fd = 1
            acquire_started.set()
            await finish_acquire.wait()
            raise backend_cancellation

        async def _release(self) -> None:  # ty: ignore[invalid-method-override]
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
        assert (lock.lock_counter, acquired.is_set()) == (1, False)
        task.cancel()
        release_executor.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        blocker.result(timeout=5)

    assert acquired.is_set()
    assert (lock.is_locked, lock.lock_counter) == (False, 0)
    assert_file_lock_state(str(tmp_path / "a"), available=True)


@_UNIX_FLOCK_ONLY
@pytest.mark.asyncio
async def test_acquire_cancellation_does_not_release_later_claim(tmp_path: Path) -> None:
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


@_UNIX_FLOCK_ONLY
@pytest.mark.asyncio
async def test_cancelled_queued_acquire_does_not_claim_transition(tmp_path: Path) -> None:
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


@_UNIX_FLOCK_ONLY
@pytest.mark.asyncio
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
    mocker.patch("filelock._unix.fcntl.flock", side_effect=block_rollback)
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


@_UNIX_FLOCK_ONLY
@pytest.mark.parametrize("policy", [pytest.param("chain", id="chain"), pytest.param("group", id="group")])
@pytest.mark.asyncio
async def test_acquire_cancellation_surfaces_attempt_error_after_rollback(
    tmp_path: Path, policy: ContextErrorPolicy
) -> None:
    hook_started = asyncio.Event()
    finish_hook = threading.Event()
    loop = asyncio.get_running_loop()
    callback_error = ValueError("hook failed")
    prior_context = LookupError("prior callback failure")
    callback_error.__context__ = prior_context

    def fail_hook(_fd: int) -> None:
        loop.call_soon_threadsafe(hook_started.set)
        assert finish_hook.wait(timeout=5)
        raise callback_error

    lock = AsyncFileLock(tmp_path / "a", context_error_policy=policy, on_acquired=fail_hook)
    task = asyncio.create_task(lock.acquire())
    await hook_started.wait()
    task.cancel("cancel acquire")
    finish_hook.set()
    if policy == "chain":
        with pytest.raises(ValueError, match="hook failed") as info:
            await task
        cancellation = info.value.__context__
        assert info.value is callback_error
    else:
        with pytest.raises(BaseExceptionGroup) as info:
            await task
        cancellation, grouped_callback_error = info.value.exceptions
        assert grouped_callback_error is callback_error
        assert (info.value.__cause__, info.value.__suppress_context__) == (None, True)
    assert isinstance(cancellation, asyncio.CancelledError)
    assert (cancellation.args, cancellation.__context__, callback_error.__cause__, callback_error.__context__) == (
        ("cancel acquire",),
        prior_context if policy == "chain" else None,
        None,
        cancellation if policy == "chain" else prior_context,
    )
    assert (lock.is_locked, lock.lock_counter) == (False, 0)
    assert_file_lock_state(str(tmp_path / "a"), available=True)


@_UNIX_FLOCK_ONLY
@pytest.mark.parametrize(
    ("context_message", "preserved"),
    [pytest.param("unrelated", True, id="distinct"), pytest.param("attempt", False, id="equivalent")],
)
@pytest.mark.asyncio
async def test_acquire_cancellation_group_reconciles_attempt_context(
    tmp_path: Path, context_message: str, *, preserved: bool
) -> None:
    hook_started = asyncio.Event()
    finish_hook = threading.Event()
    loop = asyncio.get_running_loop()
    nested_leaf = LookupError("nested")
    callback_group = ExceptionGroup("attempt", (nested_leaf,))
    prior_context = ExceptionGroup(context_message, (nested_leaf,))
    callback_group.__context__ = prior_context

    def fail_hook(_fd: int) -> None:
        loop.call_soon_threadsafe(hook_started.set)
        assert finish_hook.wait(timeout=5)
        raise callback_group

    lock = AsyncFileLock(tmp_path / "a", context_error_policy="group", on_acquired=fail_hook)
    task = asyncio.create_task(lock.acquire())
    await hook_started.wait()
    task.cancel("cancel acquire")
    finish_hook.set()
    with pytest.raises(BaseExceptionGroup) as info:
        await task

    cancellation, grouped_callback_error = info.value.exceptions
    assert isinstance(cancellation, asyncio.CancelledError)
    assert isinstance(grouped_callback_error, ExceptionGroup)
    assert grouped_callback_error is callback_group
    assert (
        type(grouped_callback_error),
        grouped_callback_error.message,
        grouped_callback_error.exceptions,
        grouped_callback_error.__context__,
        callback_group.__context__,
    ) == (
        ExceptionGroup,
        "attempt",
        (nested_leaf,),
        prior_context if preserved else None,
        prior_context if preserved else None,
    )
    assert (lock.is_locked, lock.lock_counter) == (False, 0)
    assert_file_lock_state(str(tmp_path / "a"), available=True)


@_UNIX_FLOCK_ONLY
@pytest.mark.parametrize("policy", [pytest.param("chain", id="chain"), pytest.param("group", id="group")])
@pytest.mark.asyncio
async def test_acquire_cancellation_surfaces_rollback_error(
    tmp_path: Path,
    mocker: MockerFixture,
    caplog: pytest.LogCaptureFixture,
    policy: ContextErrorPolicy,
) -> None:
    hook_started = asyncio.Event()
    finish_hook = threading.Event()
    loop = asyncio.get_running_loop()

    def block_hook(_fd: int) -> None:
        loop.call_soon_threadsafe(hook_started.set)
        assert finish_hook.wait(timeout=5)

    rollback_error = OSError(EIO, "rollback failed")
    lock = AsyncFileLock(tmp_path / "a", context_error_policy=policy, on_acquired=block_hook)
    task = asyncio.create_task(lock.acquire())
    await hook_started.wait()
    mocker.patch("filelock._unix.fcntl.flock", side_effect=[rollback_error, None])
    task.cancel("cancel acquire")
    finish_hook.set()
    if policy == "chain":
        with pytest.raises(OSError, match="rollback failed") as info:
            await task
        cancellation = info.value.__context__
        assert info.value is rollback_error
    else:
        with pytest.raises(BaseExceptionGroup) as info:
            await task
        cancellation, grouped_rollback_error = info.value.exceptions
        assert grouped_rollback_error is rollback_error
        assert (info.value.__cause__, info.value.__suppress_context__) == (None, True)
    assert isinstance(cancellation, asyncio.CancelledError)
    assert (cancellation.args, rollback_error.__cause__, rollback_error.__context__) == (
        ("cancel acquire",),
        None,
        cancellation if policy == "chain" else None,
    )
    assert (cancellation.__traceback__ is not None, rollback_error.__traceback__ is not None) == (True, True)
    assert not any(record.name == "asyncio" and record.levelno >= logging.ERROR for record in caplog.records)
    assert (lock.is_locked, lock.lock_counter) == (True, 1)
    assert_file_lock_state(str(tmp_path / "a"), available=False)
    await lock.release()


@_UNIX_FLOCK_ONLY
@pytest.mark.parametrize("policy", [pytest.param("chain", id="chain"), pytest.param("group", id="group")])
@pytest.mark.asyncio
async def test_acquire_cancellation_surfaces_attempt_and_rollback_errors(
    tmp_path: Path, mocker: MockerFixture, policy: ContextErrorPolicy
) -> None:
    hook_started = asyncio.Event()
    finish_hook = threading.Event()
    loop = asyncio.get_running_loop()
    callback_error = ValueError("hook failed")

    def fail_hook(_fd: int) -> None:
        loop.call_soon_threadsafe(hook_started.set)
        assert finish_hook.wait(timeout=5)
        raise callback_error

    first_rollback_error = OSError(EIO, "hook rollback failed")
    second_rollback_error = OSError(EIO, "cancellation rollback failed")
    lock = AsyncFileLock(tmp_path / "a", context_error_policy=policy, on_acquired=fail_hook)
    task = asyncio.create_task(lock.acquire())
    await hook_started.wait()
    mocker.patch(
        "filelock._unix.fcntl.flock",
        side_effect=[first_rollback_error, second_rollback_error, None],
    )
    task.cancel("cancel acquire")
    finish_hook.set()
    if policy == "chain":
        with pytest.raises(OSError, match="cancellation rollback failed") as info:
            await task
        attempt_error = info.value.__context__
        cancellation = attempt_error.__context__ if attempt_error is not None else None
        assert info.value is second_rollback_error
    else:
        with pytest.raises(BaseExceptionGroup) as info:
            await task
        cancellation, attempt_error, grouped_rollback_error = info.value.exceptions
        assert grouped_rollback_error is second_rollback_error
        assert (info.value.__cause__, info.value.__suppress_context__) == (None, True)
    assert isinstance(attempt_error, BaseExceptionGroup)
    assert (attempt_error.exceptions[0], attempt_error.exceptions[1]) == (callback_error, first_rollback_error)
    assert isinstance(cancellation, asyncio.CancelledError)
    assert (
        cancellation.args,
        attempt_error.__cause__,
        attempt_error.__context__,
        second_rollback_error.__cause__,
        second_rollback_error.__context__,
    ) == (
        ("cancel acquire",),
        None,
        cancellation if policy == "chain" else None,
        None,
        attempt_error if policy == "chain" else None,
    )
    assert (callback_error.__context__, first_rollback_error.__context__) == (None, None)
    assert (
        cancellation.__traceback__ is not None,
        callback_error.__traceback__ is not None,
        first_rollback_error.__traceback__ is not None,
        attempt_error.__traceback__ is not None,
        second_rollback_error.__traceback__ is not None,
    ) == (True, True, True, True, True)
    assert (lock.is_locked, lock.lock_counter) == (True, 1)
    assert_file_lock_state(str(tmp_path / "a"), available=False)
    await lock.release()


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


def _block_executor(executor_started: threading.Event, release_executor: threading.Event) -> None:
    executor_started.set()
    assert release_executor.wait(timeout=5)
