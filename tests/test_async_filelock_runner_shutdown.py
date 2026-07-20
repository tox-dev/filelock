from __future__ import annotations

import asyncio
import sys
import threading
from errno import EIO
from importlib.util import find_spec
from queue import Queue
from typing import TYPE_CHECKING, Final, TypeVar

import pytest
from async_filelock_cancellation_helpers import assert_file_lock_state, get_fcntl
from capability_marks import XFAIL_WITHOUT_COROUTINE_CANCELLATION

from filelock import AsyncAcquireReturnProxy, AsyncFileLock, ContextErrorPolicy

if sys.version_info >= (3, 11):  # pragma: no cover (py311+)
    from builtins import BaseExceptionGroup  # pragma: >=3.11 cover
else:  # pragma: no cover (<py311)
    from exceptiongroup import BaseExceptionGroup

if TYPE_CHECKING:
    from collections.abc import Coroutine
    from pathlib import Path

    from pytest_mock import MockerFixture

_NEEDS_FCNTL: Final[pytest.MarkDecorator] = pytest.mark.skipif(
    find_spec("fcntl") is None, reason="native flock semantics come from the fcntl module"
)
_T = TypeVar("_T")


class _CancellationObservedTask(asyncio.Task[_T]):  # pragma: needs fcntl
    def __init__(
        self,
        coroutine: Coroutine[None, None, _T],
        cancellation_seen: threading.Event,
        *,
        loop: asyncio.AbstractEventLoop,
    ) -> None:
        self._cancellation_seen = cancellation_seen
        super().__init__(coroutine, loop=loop)

    def cancel(self, msg: object | None = None) -> bool:
        # Task.cancel accepts arbitrary payloads; object is the narrowest accurate type for its public contract.
        self._cancellation_seen.set()
        return super().cancel(msg)


@_NEEDS_FCNTL
def test_runner_shutdown_waits_for_executor_acquire_rollback(tmp_path: Path) -> None:  # pragma: needs fcntl
    hook_started = threading.Event()
    finish_hook = threading.Event()
    cancellation_seen = threading.Event()

    def block_hook(_fd: int) -> None:
        hook_started.set()
        assert finish_hook.wait(timeout=5)

    lock = AsyncFileLock(tmp_path / "a", on_acquired=block_hook)
    tasks: Queue[asyncio.Task[AsyncAcquireReturnProxy]] = Queue()
    runner = threading.Thread(
        target=_run_unawaited_acquire,
        args=(lock, hook_started, cancellation_seen, tasks),
    )
    runner.start()
    try:
        tasks.get(timeout=5)
        assert hook_started.wait(timeout=5)
        assert cancellation_seen.wait(timeout=5), "asyncio runner did not cancel the pending lock task"
    finally:
        finish_hook.set()
        runner.join(timeout=5)

    assert (runner.is_alive(), lock.is_locked, lock.lock_counter) == (False, False, 0)
    assert_file_lock_state(str(tmp_path / "a"), available=True)


@_NEEDS_FCNTL
@pytest.mark.parametrize("policy", [pytest.param("chain", id="chain"), pytest.param("group", id="group")])
@XFAIL_WITHOUT_COROUTINE_CANCELLATION
def test_runner_shutdown_preserves_body_cancellation_and_release_errors(  # pragma: needs fcntl
    tmp_path: Path, mocker: MockerFixture, policy: ContextErrorPolicy
) -> None:
    body_error = ValueError("body failed")
    prior_error = LookupError("prior release failure")
    release_error = OSError(EIO, "release failed")
    release_error.__context__ = prior_error
    release_started = threading.Event()
    finish_release = threading.Event()
    failed = False
    fcntl = get_fcntl()
    real_flock = fcntl.flock

    def fail_first_unlock(fd: int, operation: int) -> None:
        nonlocal failed
        if operation & fcntl.LOCK_UN and not failed:
            failed = True
            release_started.set()
            assert finish_release.wait(timeout=5)
            raise release_error
        real_flock(fd, operation)

    mocker.patch("filelock._unix.fcntl.flock", side_effect=fail_first_unlock)
    lock = AsyncFileLock(tmp_path / "a", thread_local=False, context_error_policy=policy)
    runner, task, cancellation_seen = _start_unawaited_context_failure(
        lock,
        body_error,
        release_started,
    )
    try:
        assert release_started.wait(timeout=5)
        assert cancellation_seen.wait(timeout=5), "asyncio runner did not cancel the pending lock task"
    finally:
        finish_release.set()
        runner.join(timeout=5)

    assert (runner.is_alive(), task.done()) == (False, True)
    error = task.exception()
    if policy == "chain":
        assert error is release_error
        cancellation = release_error.__context__
        assert isinstance(cancellation, asyncio.CancelledError)
        assert (cancellation.__context__, prior_error.__context__) == (prior_error, body_error)
    else:
        assert isinstance(error, BaseExceptionGroup)
        cancellation = error.exceptions[1]
        assert isinstance(cancellation, asyncio.CancelledError)
        assert error.exceptions == (body_error, cancellation, release_error)
        assert (cancellation.__context__, release_error.__context__, prior_error.__context__) == (
            None,
            prior_error,
            None,
        )
    assert (lock.is_locked, lock.lock_counter) == (True, 1)
    assert_file_lock_state(str(tmp_path / "a"), available=False)
    asyncio.run(lock.release())
    assert_file_lock_state(str(tmp_path / "a"), available=True)


def _run_unawaited_acquire(  # pragma: needs fcntl
    lock: AsyncFileLock,
    hook_started: threading.Event,
    cancellation_seen: threading.Event,
    tasks: Queue[asyncio.Task[AsyncAcquireReturnProxy]],
) -> None:
    async def start_acquire() -> None:
        tasks.put(
            _CancellationObservedTask(
                lock.acquire(),
                cancellation_seen,
                loop=asyncio.get_running_loop(),
            )
        )
        assert await asyncio.to_thread(hook_started.wait, 5)

    asyncio.run(start_acquire())


def _start_unawaited_context_failure(  # pragma: needs fcntl
    lock: AsyncFileLock,
    body_error: BaseException,
    release_started: threading.Event,
) -> tuple[threading.Thread, asyncio.Task[None], threading.Event]:
    tasks: Queue[asyncio.Task[None]] = Queue()
    cancellation_seen = threading.Event()
    runner = threading.Thread(
        target=_run_unawaited_context_failure,
        args=(lock, body_error, release_started, cancellation_seen, tasks),
    )
    runner.start()
    task = tasks.get(timeout=5)
    return runner, task, cancellation_seen


def _run_unawaited_context_failure(  # pragma: needs fcntl
    lock: AsyncFileLock,
    body_error: BaseException,
    release_started: threading.Event,
    cancellation_seen: threading.Event,
    tasks: Queue[asyncio.Task[None]],
) -> None:
    async def fail_in_context() -> None:
        async with lock:
            raise body_error

    async def start_context() -> None:
        tasks.put(
            _CancellationObservedTask(
                fail_in_context(),
                cancellation_seen,
                loop=asyncio.get_running_loop(),
            )
        )
        assert await asyncio.to_thread(release_started.wait, 5)

    asyncio.run(start_context())
