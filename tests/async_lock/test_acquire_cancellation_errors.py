from __future__ import annotations

import asyncio
import logging
import threading
from errno import EIO
from typing import TYPE_CHECKING

import pytest

from filelock import (
    AsyncAcquireReturnProxy,
    AsyncFileLock,
    ContextErrorPolicy,
)
from tests.async_lock.cancellation import (
    assert_file_lock_state,
)
from tests.capability_marks import NEEDS_FCNTL, XFAIL_WITHOUT_COROUTINE_CANCELLATION

if TYPE_CHECKING:
    from pathlib import Path

    from pytest_mock import MockerFixture


@NEEDS_FCNTL
@pytest.mark.asyncio  # pragma: needs fcntl
@XFAIL_WITHOUT_COROUTINE_CANCELLATION
async def test_acquire_cancellation_chains_attempt_error_after_rollback(tmp_path: Path) -> None:
    callback_error = ValueError("hook failed")
    prior_context = LookupError("prior callback failure")
    callback_error.__context__ = prior_context
    hook_started = asyncio.Event()
    finish_hook = threading.Event()
    lock, task = await _cancel_acquire_in_hook(tmp_path, "chain", callback_error, hook_started, finish_hook)
    finish_hook.set()
    with pytest.raises(ValueError, match="hook failed") as info:
        await task
    cancellation = info.value.__context__
    assert info.value is callback_error
    assert isinstance(cancellation, asyncio.CancelledError)
    assert (cancellation.args, cancellation.__context__, callback_error.__cause__, callback_error.__context__) == (
        ("cancel acquire",),
        prior_context,
        None,
        cancellation,
    )
    assert (lock.is_locked, lock.lock_counter) == (False, 0)
    assert_file_lock_state(str(tmp_path / "a"), available=True)


async def _cancel_acquire_in_hook(  # pragma: needs fcntl
    tmp_path: Path,
    policy: ContextErrorPolicy,
    hook_error: BaseException | None,
    hook_started: asyncio.Event,
    finish_hook: threading.Event,
) -> tuple[AsyncFileLock, asyncio.Task[AsyncAcquireReturnProxy]]:
    loop = asyncio.get_running_loop()

    def hook(_fd: int) -> None:
        loop.call_soon_threadsafe(hook_started.set)
        assert finish_hook.wait(timeout=5)
        if hook_error is not None:
            raise hook_error

    lock = AsyncFileLock(tmp_path / "a", context_error_policy=policy, on_acquired=hook)
    task = asyncio.create_task(lock.acquire())
    await hook_started.wait()
    task.cancel("cancel acquire")
    return lock, task


@NEEDS_FCNTL
@pytest.mark.asyncio  # pragma: needs fcntl
@XFAIL_WITHOUT_COROUTINE_CANCELLATION
async def test_acquire_cancellation_groups_attempt_error_after_rollback(tmp_path: Path) -> None:
    callback_error = ValueError("hook failed")
    prior_context = LookupError("prior callback failure")
    callback_error.__context__ = prior_context
    hook_started = asyncio.Event()
    finish_hook = threading.Event()
    lock, task = await _cancel_acquire_in_hook(tmp_path, "group", callback_error, hook_started, finish_hook)
    finish_hook.set()
    with pytest.raises(BaseExceptionGroup) as info:
        await task
    cancellation, grouped_callback_error = info.value.exceptions
    assert grouped_callback_error is callback_error
    assert (info.value.__cause__, info.value.__suppress_context__) == (None, True)
    assert isinstance(cancellation, asyncio.CancelledError)
    assert (cancellation.args, cancellation.__context__, callback_error.__cause__, callback_error.__context__) == (
        ("cancel acquire",),
        None,
        None,
        prior_context,
    )
    assert (lock.is_locked, lock.lock_counter) == (False, 0)
    assert_file_lock_state(str(tmp_path / "a"), available=True)


@NEEDS_FCNTL
@pytest.mark.asyncio  # pragma: needs fcntl
@XFAIL_WITHOUT_COROUTINE_CANCELLATION
async def test_acquire_cancellation_group_keeps_distinct_attempt_context(tmp_path: Path) -> None:
    nested_leaf = LookupError("nested")
    callback_group = ExceptionGroup("attempt", (nested_leaf,))
    prior_context = ExceptionGroup("unrelated", (nested_leaf,))
    callback_group.__context__ = prior_context
    hook_started = asyncio.Event()
    finish_hook = threading.Event()
    lock, task = await _cancel_acquire_in_hook(tmp_path, "group", callback_group, hook_started, finish_hook)
    finish_hook.set()
    with pytest.raises(BaseExceptionGroup) as info:
        await task

    cancellation, grouped_callback_error = info.value.exceptions
    assert isinstance(cancellation, asyncio.CancelledError)
    assert grouped_callback_error is callback_group
    assert (
        type(grouped_callback_error),
        grouped_callback_error.message,
        grouped_callback_error.exceptions,
        grouped_callback_error.__context__,
        callback_group.__context__,
    ) == (ExceptionGroup, "attempt", (nested_leaf,), prior_context, prior_context)
    assert (lock.is_locked, lock.lock_counter) == (False, 0)
    assert_file_lock_state(str(tmp_path / "a"), available=True)


@NEEDS_FCNTL
@pytest.mark.asyncio  # pragma: needs fcntl
@XFAIL_WITHOUT_COROUTINE_CANCELLATION
async def test_acquire_cancellation_group_drops_equivalent_attempt_context(tmp_path: Path) -> None:
    nested_leaf = LookupError("nested")
    callback_group = ExceptionGroup("attempt", (nested_leaf,))
    callback_group.__context__ = ExceptionGroup("attempt", (nested_leaf,))
    hook_started = asyncio.Event()
    finish_hook = threading.Event()
    lock, task = await _cancel_acquire_in_hook(tmp_path, "group", callback_group, hook_started, finish_hook)
    finish_hook.set()
    with pytest.raises(BaseExceptionGroup) as info:
        await task

    cancellation, grouped_callback_error = info.value.exceptions
    assert isinstance(cancellation, asyncio.CancelledError)
    assert grouped_callback_error is callback_group
    assert (
        type(grouped_callback_error),
        grouped_callback_error.message,
        grouped_callback_error.exceptions,
        grouped_callback_error.__context__,
        callback_group.__context__,
    ) == (ExceptionGroup, "attempt", (nested_leaf,), None, None)
    assert (lock.is_locked, lock.lock_counter) == (False, 0)
    assert_file_lock_state(str(tmp_path / "a"), available=True)


@NEEDS_FCNTL
@pytest.mark.asyncio  # pragma: needs fcntl
@XFAIL_WITHOUT_COROUTINE_CANCELLATION
async def test_acquire_cancellation_chains_rollback_error(
    tmp_path: Path, mocker: MockerFixture, caplog: pytest.LogCaptureFixture
) -> None:
    rollback_error = OSError(EIO, "rollback failed")
    hook_started = asyncio.Event()
    finish_hook = threading.Event()
    lock, task = await _cancel_acquire_in_hook(tmp_path, "chain", None, hook_started, finish_hook)
    mocker.patch("filelock._native._unix.fcntl.flock", side_effect=[rollback_error, None], autospec=True)
    finish_hook.set()
    with pytest.raises(OSError, match="rollback failed") as info:
        await task
    cancellation = info.value.__context__
    assert info.value is rollback_error
    assert isinstance(cancellation, asyncio.CancelledError)
    assert (cancellation.args, rollback_error.__cause__, rollback_error.__context__) == (
        ("cancel acquire",),
        None,
        cancellation,
    )
    assert (cancellation.__traceback__ is not None, rollback_error.__traceback__ is not None) == (True, True)
    assert not any(record.name == "asyncio" and record.levelno >= logging.ERROR for record in caplog.records)
    assert (lock.is_locked, lock.lock_counter) == (True, 1)
    assert_file_lock_state(str(tmp_path / "a"), available=False)
    await lock.release()


@NEEDS_FCNTL
@pytest.mark.asyncio  # pragma: needs fcntl
@XFAIL_WITHOUT_COROUTINE_CANCELLATION
async def test_acquire_cancellation_groups_rollback_error(
    tmp_path: Path, mocker: MockerFixture, caplog: pytest.LogCaptureFixture
) -> None:
    rollback_error = OSError(EIO, "rollback failed")
    hook_started = asyncio.Event()
    finish_hook = threading.Event()
    lock, task = await _cancel_acquire_in_hook(tmp_path, "group", None, hook_started, finish_hook)
    mocker.patch("filelock._native._unix.fcntl.flock", side_effect=[rollback_error, None], autospec=True)
    finish_hook.set()
    with pytest.raises(BaseExceptionGroup) as info:
        await task
    cancellation, grouped_rollback_error = info.value.exceptions
    assert grouped_rollback_error is rollback_error
    assert (info.value.__cause__, info.value.__suppress_context__) == (None, True)
    assert isinstance(cancellation, asyncio.CancelledError)
    assert (cancellation.args, rollback_error.__cause__, rollback_error.__context__) == (
        ("cancel acquire",),
        None,
        None,
    )
    assert (cancellation.__traceback__ is not None, rollback_error.__traceback__ is not None) == (True, True)
    assert not any(record.name == "asyncio" and record.levelno >= logging.ERROR for record in caplog.records)
    assert (lock.is_locked, lock.lock_counter) == (True, 1)
    assert_file_lock_state(str(tmp_path / "a"), available=False)
    await lock.release()


@NEEDS_FCNTL
@pytest.mark.asyncio  # pragma: needs fcntl
@XFAIL_WITHOUT_COROUTINE_CANCELLATION
async def test_acquire_cancellation_chains_attempt_and_rollback_errors(tmp_path: Path, mocker: MockerFixture) -> None:
    callback_error = ValueError("hook failed")
    first_rollback_error = OSError(EIO, "hook rollback failed")
    second_rollback_error = OSError(EIO, "cancellation rollback failed")
    hook_started = asyncio.Event()
    finish_hook = threading.Event()
    lock, task = await _cancel_acquire_in_hook(tmp_path, "chain", callback_error, hook_started, finish_hook)
    mocker.patch(
        "filelock._native._unix.fcntl.flock",
        side_effect=[first_rollback_error, second_rollback_error, None],
        autospec=True,
    )
    finish_hook.set()
    with pytest.raises(OSError, match="cancellation rollback failed") as info:
        await task
    attempt_error = info.value.__context__
    assert isinstance(attempt_error, BaseExceptionGroup)
    cancellation = attempt_error.__context__
    assert info.value is second_rollback_error
    assert (attempt_error.exceptions[0], attempt_error.exceptions[1]) == (callback_error, first_rollback_error)
    assert isinstance(cancellation, asyncio.CancelledError)
    assert (
        cancellation.args,
        attempt_error.__cause__,
        attempt_error.__context__,
        second_rollback_error.__cause__,
        second_rollback_error.__context__,
    ) == (("cancel acquire",), None, cancellation, None, attempt_error)
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


@NEEDS_FCNTL
@pytest.mark.asyncio  # pragma: needs fcntl
@XFAIL_WITHOUT_COROUTINE_CANCELLATION
async def test_acquire_cancellation_groups_attempt_and_rollback_errors(tmp_path: Path, mocker: MockerFixture) -> None:
    callback_error = ValueError("hook failed")
    first_rollback_error = OSError(EIO, "hook rollback failed")
    second_rollback_error = OSError(EIO, "cancellation rollback failed")
    hook_started = asyncio.Event()
    finish_hook = threading.Event()
    lock, task = await _cancel_acquire_in_hook(tmp_path, "group", callback_error, hook_started, finish_hook)
    mocker.patch(
        "filelock._native._unix.fcntl.flock",
        side_effect=[first_rollback_error, second_rollback_error, None],
        autospec=True,
    )
    finish_hook.set()
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
    ) == (("cancel acquire",), None, None, None, None)
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
