from __future__ import annotations

import asyncio
import sqlite3
import threading
from concurrent.futures import Executor, ThreadPoolExecutor
from typing import TYPE_CHECKING, Final, Literal

import pytest

from filelock import AsyncReadWriteLock
from tests.async_lock.cancellation import assert_cancellation_message
from tests.capability_marks import XFAIL_WITHOUT_COROUTINE_CANCELLATION
from tests.read_write_helpers import OPPOSITE_MODE, assert_read_write_lock_state
from tests.sqlite.async_helpers import fail_rollback, patch_connection, pause_rollback

if TYPE_CHECKING:
    from collections.abc import Callable

    from pytest_mock import MockerFixture


# Each peer probe starts a process and waits for it to exit.
pytestmark: Final[pytest.MarkDecorator] = pytest.mark.timeout(120)


@pytest.mark.asyncio
async def test_close_cancellation_leaves_a_supplied_executor_running(lock_file: str, mocker: MockerFixture) -> None:
    await _cancel_close_of_write_lock(
        lock_file, mocker, ThreadPoolExecutor(max_workers=1), _assert_supplied_executor_runs
    )


async def _cancel_close_of_write_lock(
    lock_file: str,
    mocker: MockerFixture,
    executor: Executor | None,
    verify: Callable[[Executor], None],
) -> None:
    rollback_started = asyncio.Event()
    finish_rollback = threading.Event()
    pause_rollback(mocker, asyncio.get_running_loop(), rollback_started, finish_rollback)
    lock = AsyncReadWriteLock(lock_file, is_singleton=False, executor=executor)
    await lock.acquire_write()
    owned = lock.executor
    task = asyncio.create_task(lock.close())
    await rollback_started.wait()
    task.cancel("cancel close")
    finish_rollback.set()

    with pytest.raises(asyncio.CancelledError) as info:
        await task
    assert_cancellation_message(info.value, "cancel close")
    assert_read_write_lock_state(lock_file, "read", available=True)
    verify(owned)


def _assert_supplied_executor_runs(executor: Executor) -> None:
    assert executor.submit(int).result(timeout=5) == 0
    executor.shutdown(wait=True)


@pytest.mark.asyncio
async def test_close_cancellation_shuts_down_an_owned_executor(lock_file: str, mocker: MockerFixture) -> None:
    await _cancel_close_of_write_lock(lock_file, mocker, None, _assert_owned_executor_is_shut_down)


def _assert_owned_executor_is_shut_down(executor: Executor) -> None:
    with pytest.raises(RuntimeError):
        executor.submit(int)


@pytest.mark.parametrize("mode", [pytest.param("read", id="read"), pytest.param("write", id="write")])
@pytest.mark.asyncio
@XFAIL_WITHOUT_COROUTINE_CANCELLATION
async def test_release_cancellation_hands_the_lock_to_the_next_task(
    lock_file: str, mocker: MockerFixture, mode: Literal["read", "write"]
) -> None:
    rollback_started = asyncio.Event()
    finish_rollback = threading.Event()
    pause_rollback(mocker, asyncio.get_running_loop(), rollback_started, finish_rollback)
    lock = AsyncReadWriteLock(lock_file, is_singleton=False)

    async def acquire_then_release() -> None:
        await getattr(lock, f"acquire_{mode}")()
        await lock.release()

    task = asyncio.create_task(acquire_then_release())
    await rollback_started.wait()
    task.cancel("cancel release")
    finish_rollback.set()
    with pytest.raises(asyncio.CancelledError) as info:
        await task
    assert_cancellation_message(info.value, "cancel release")

    await lock.acquire_write(timeout=2)
    assert_read_write_lock_state(lock_file, "read", available=False)
    await lock.release()
    await lock.close()


@pytest.mark.parametrize("operation", [pytest.param("release", id="release"), pytest.param("close", id="close")])
@pytest.mark.asyncio
@XFAIL_WITHOUT_COROUTINE_CANCELLATION
async def test_cancellation_surfaces_rollback_error(
    lock_file: str, mocker: MockerFixture, operation: Literal["release", "close"]
) -> None:
    rollback_started = asyncio.Event()
    finish_rollback = threading.Event()
    rollback_error = fail_rollback(mocker, asyncio.get_running_loop(), rollback_started, finish_rollback)
    lock = AsyncReadWriteLock(lock_file, is_singleton=False)
    executor = lock.executor

    async def acquire_then_finish() -> None:
        await lock.acquire_write()
        await (lock.release() if operation == "release" else lock.close())

    task = asyncio.create_task(acquire_then_finish())
    await rollback_started.wait()
    task.cancel(f"cancel {operation}")
    finish_rollback.set()

    with pytest.raises(sqlite3.OperationalError, match="rollback failed") as info:
        await task
    assert info.value is rollback_error
    cancellation = rollback_error.__context__
    assert isinstance(cancellation, asyncio.CancelledError)
    assert cancellation.args == (f"cancel {operation}",)
    assert_read_write_lock_state(lock_file, "read", available=False)
    assert executor.submit(int).result(timeout=5) == 0

    await lock.close()
    assert_read_write_lock_state(lock_file, "read", available=True)
    with pytest.raises(RuntimeError):
        executor.submit(int)


@pytest.mark.parametrize("mode", [pytest.param("read", id="read"), pytest.param("write", id="write")])
@pytest.mark.asyncio
@XFAIL_WITHOUT_COROUTINE_CANCELLATION
async def test_context_cancellation_preserves_body_and_rollback_contexts(
    lock_file: str, mocker: MockerFixture, mode: Literal["read", "write"]
) -> None:
    rollback_started = asyncio.Event()
    finish_rollback = threading.Event()
    body_error = ValueError("body failed")
    prior_error = LookupError("prior rollback failure")
    rollback_error = fail_rollback(
        mocker,
        asyncio.get_running_loop(),
        rollback_started,
        finish_rollback,
        prior_context=prior_error,
    )
    lock = AsyncReadWriteLock(lock_file, is_singleton=False)

    async def fail_in_context() -> None:
        async with lock.read_lock() if mode == "read" else lock.write_lock():
            raise body_error

    task = asyncio.create_task(fail_in_context())
    await rollback_started.wait()
    task.cancel("cancel release")
    finish_rollback.set()

    with pytest.raises(sqlite3.OperationalError, match="rollback failed") as info:
        await task
    cancellation = rollback_error.__context__
    assert isinstance(cancellation, asyncio.CancelledError)
    assert (info.value, cancellation.args, cancellation.__context__, prior_error.__context__) == (
        rollback_error,
        ("cancel release",),
        prior_error,
        body_error,
    )
    probe_mode: Literal["read", "write"] = OPPOSITE_MODE[mode]
    assert_read_write_lock_state(lock_file, probe_mode, available=False)

    await lock.close()
    assert_read_write_lock_state(lock_file, probe_mode, available=True)


@pytest.mark.asyncio
async def test_read_retried_after_its_select_waited_starts_a_fresh_transaction(
    lock_file: str, mocker: MockerFixture
) -> None:
    busy_once = True
    seen_in_transaction: list[bool] = []

    def execute(real_connection: sqlite3.Connection, statement: str) -> sqlite3.Cursor:
        nonlocal busy_once
        if statement == "BEGIN TRANSACTION":
            seen_in_transaction.append(real_connection.in_transaction)
        if "SELECT name" in statement and busy_once:
            # A writer can take the lock between the deferred BEGIN and the SELECT that needs the shared lock.
            busy_once = False
            raise _busy_error()
        return real_connection.execute(statement)

    patch_connection(mocker, execute=execute)
    # With other threads alive the acquire waits in short slices that never reach the in-place retry under test.
    mocker.patch.object(threading, "active_count", return_value=1, autospec=True)
    lock = AsyncReadWriteLock(lock_file, is_singleton=False)

    async with lock.read_lock():
        assert_read_write_lock_state(lock_file, "write", available=False)
    await lock.close()
    assert seen_in_transaction == [False, False]


def _busy_error() -> sqlite3.OperationalError:
    error = sqlite3.OperationalError("database is locked")
    error.sqlite_errorcode = sqlite3.SQLITE_BUSY
    return error


@pytest.mark.asyncio
async def test_write_retried_after_its_begin_waited_is_acquired(lock_file: str, mocker: MockerFixture) -> None:
    busy_once = True

    def execute(real_connection: sqlite3.Connection, statement: str) -> sqlite3.Cursor:
        nonlocal busy_once
        if "BEGIN EXCLUSIVE" in statement and busy_once:
            busy_once = False
            raise _busy_error()
        return real_connection.execute(statement)

    patch_connection(mocker, execute=execute)
    mocker.patch.object(threading, "active_count", return_value=1, autospec=True)
    lock = AsyncReadWriteLock(lock_file, is_singleton=False)

    async with lock.write_lock():
        assert_read_write_lock_state(lock_file, "read", available=False)
    await lock.close()
    assert not busy_once
