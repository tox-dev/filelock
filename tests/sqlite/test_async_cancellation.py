from __future__ import annotations

import asyncio
import multiprocessing
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING, Final, Literal, NamedTuple

import pytest

from filelock import AsyncReadWriteLock, ReadWriteLock
from tests.async_lock.cancellation import assert_cancellation_message
from tests.capability_marks import XFAIL_WITHOUT_COROUTINE_CANCELLATION
from tests.read_write_helpers import OPPOSITE_MODE, assert_read_write_lock_state
from tests.sqlite.async_helpers import fail_rollback, patch_connection, pause_rollback

if TYPE_CHECKING:
    from multiprocessing.sharedctypes import Synchronized

    from pytest_mock import MockerFixture


# Each peer probe starts a process and waits for it to exit.
pytestmark: Final[pytest.MarkDecorator] = pytest.mark.timeout(120)


@pytest.mark.parametrize("mode", [pytest.param("read", id="read"), pytest.param("write", id="write")])
@pytest.mark.asyncio
async def test_acquire_cancellation_before_executor_start_rolls_back(
    lock_file: str, mocker: MockerFixture, mode: Literal["read", "write"]
) -> None:
    executor_started = threading.Event()
    release_executor = threading.Event()
    rollback_started = asyncio.Event()
    finish_rollback = threading.Event()
    pause_rollback(mocker, asyncio.get_running_loop(), rollback_started, finish_rollback)

    with ThreadPoolExecutor(max_workers=1) as executor:
        blocker = executor.submit(_block_executor, executor_started, release_executor)
        assert executor_started.wait(timeout=5)
        lock = AsyncReadWriteLock(lock_file, is_singleton=False, executor=executor)
        task = asyncio.create_task(getattr(lock, f"acquire_{mode}")())
        await asyncio.sleep(0)
        task.cancel("first cancellation")
        release_executor.set()
        await rollback_started.wait()
        try:
            assert_read_write_lock_state(lock_file, OPPOSITE_MODE[mode], available=False)
            task.cancel("second cancellation")
        finally:
            finish_rollback.set()
        with pytest.raises(asyncio.CancelledError) as info:
            await task
        blocker.result(timeout=5)
        assert_cancellation_message(info.value, "first cancellation")
        assert_read_write_lock_state(lock_file, OPPOSITE_MODE[mode], available=True)
        with pytest.raises(RuntimeError, match="not held"):
            await lock.release()
        await lock.close()


def _block_executor(executor_started: threading.Event, release_executor: threading.Event) -> None:
    executor_started.set()
    assert release_executor.wait(timeout=5)


@pytest.mark.parametrize(
    "cancel_caller",
    [pytest.param(False, id="executor"), pytest.param(True, id="caller-and-executor")],
)
@XFAIL_WITHOUT_COROUTINE_CANCELLATION
def test_executor_cancels_queued_acquire_without_compensation(lock_file: str, *, cancel_caller: bool) -> None:
    context = multiprocessing.get_context("spawn")
    canceled = context.Value("b", False)
    process = context.Process(target=_cancel_queued_read_write_acquire, args=(lock_file, cancel_caller, canceled))
    process.start()
    try:
        process.join(timeout=10)
        assert (process.exitcode, canceled.value) == (0, True)
    finally:
        process.terminate()
        process.join(timeout=5)
        process.close()


def _cancel_queued_read_write_acquire(lock_file: str, cancel_caller: bool, canceled: Synchronized[bool]) -> None:
    async def cancel_queued_acquire() -> None:
        executor_started = threading.Event()
        release_executor = threading.Event()
        executor = ThreadPoolExecutor(max_workers=1)
        blocker = executor.submit(_block_executor, executor_started, release_executor)
        assert executor_started.wait(timeout=5)
        task = asyncio.create_task(AsyncReadWriteLock(lock_file, is_singleton=False, executor=executor).acquire_write())
        await asyncio.sleep(0)
        if cancel_caller:
            task.cancel("caller canceled")
            await asyncio.sleep(0)
        executor.shutdown(wait=False, cancel_futures=True)
        release_executor.set()
        blocker.result(timeout=5)
        try:
            await task
        except asyncio.CancelledError as error:
            context = error.__context__
            if cancel_caller:
                canceled.value = isinstance(context, asyncio.CancelledError) and context.args == ("caller canceled",)
            else:
                canceled.value = True
        contender = ReadWriteLock(lock_file, is_singleton=False)
        with contender.write_lock(blocking=False):
            pass
        contender.close()

    asyncio.run(cancel_queued_acquire())


@pytest.mark.asyncio
@XFAIL_WITHOUT_COROUTINE_CANCELLATION
async def test_caller_cancellation_preserves_cancelled_executor_acquire(lock_file: str) -> None:
    executor_started = threading.Event()
    release_executor = threading.Event()
    executor = ThreadPoolExecutor(max_workers=1)
    blocker = executor.submit(_block_executor, executor_started, release_executor)
    assert executor_started.wait(timeout=5)
    lock = AsyncReadWriteLock(lock_file, executor=executor)
    try:
        task = asyncio.create_task(lock.acquire_write())
        await asyncio.sleep(0)
        task.cancel("caller canceled")
        await asyncio.sleep(0)
        executor.shutdown(wait=False, cancel_futures=True)
        release_executor.set()
        blocker.result(timeout=5)

        with pytest.raises(asyncio.CancelledError) as info:
            await task
        assert isinstance(info.value.__context__, asyncio.CancelledError)
        assert_cancellation_message(info.value.__context__, "caller canceled")
        contender = ReadWriteLock(lock_file, is_singleton=False)
        try:
            with contender.write_lock(blocking=False):
                pass
        finally:
            contender.close()
    finally:
        release_executor.set()
        executor.shutdown(wait=True, cancel_futures=True)
        ReadWriteLock(lock_file).close()


@pytest.mark.asyncio
@XFAIL_WITHOUT_COROUTINE_CANCELLATION
async def test_acquire_cancellation_surfaces_compensation_failure(lock_file: str, mocker: MockerFixture) -> None:
    executor_started = threading.Event()
    release_executor = threading.Event()
    rollback_started = asyncio.Event()
    finish_rollback = threading.Event()
    rollback_error = fail_rollback(mocker, asyncio.get_running_loop(), rollback_started, finish_rollback)

    with ThreadPoolExecutor(max_workers=1) as executor:
        blocker = executor.submit(_block_executor, executor_started, release_executor)
        assert executor_started.wait(timeout=5)
        lock = AsyncReadWriteLock(lock_file, is_singleton=False, executor=executor)
        task = asyncio.create_task(lock.acquire_write())
        await asyncio.sleep(0)
        task.cancel("cancel acquire")
        release_executor.set()
        await rollback_started.wait()
        finish_rollback.set()
        with pytest.raises(sqlite3.OperationalError, match="rollback failed") as info:
            await task
        blocker.result(timeout=5)
        assert info.value is rollback_error
        cancellation = rollback_error.__context__
        assert isinstance(cancellation, asyncio.CancelledError)
        assert cancellation.args == ("cancel acquire",)
        assert_read_write_lock_state(lock_file, "read", available=False)

        await lock.close()
        assert_read_write_lock_state(lock_file, "read", available=True)


@pytest.mark.parametrize("mode", [pytest.param("read", id="read"), pytest.param("write", id="write")])
@pytest.mark.asyncio
async def test_acquire_cancellation_while_sqlite_waits_rolls_back(
    lock_file: str, mocker: MockerFixture, mode: Literal["read", "write"]
) -> None:
    holder: Final[ReadWriteLock] = ReadWriteLock(lock_file, is_singleton=False)
    try:
        getattr(holder, f"acquire_{OPPOSITE_MODE[mode]}")(blocking=False)
        execute_started: Final[asyncio.Event] = asyncio.Event()
        loop = asyncio.get_running_loop()

        def execute(real_connection: sqlite3.Connection, statement: str) -> sqlite3.Cursor:
            if "PRAGMA journal_mode" in statement:
                loop.call_soon_threadsafe(execute_started.set)
            return real_connection.execute(statement)

        patch_connection(mocker, execute=execute)
        lock: Final[AsyncReadWriteLock] = AsyncReadWriteLock(lock_file, is_singleton=False)
        task = asyncio.create_task(getattr(lock, f"acquire_{mode}")())
        await execute_started.wait()
        task.cancel("cancel blocked acquire")
        holder.release()
        with pytest.raises(asyncio.CancelledError) as info:
            await task
        assert_cancellation_message(info.value, "cancel blocked acquire")
        assert_read_write_lock_state(lock_file, OPPOSITE_MODE[mode], available=True)
        with pytest.raises(RuntimeError, match="not held"):
            await lock.release()
        await lock.close()
    finally:
        holder.close()


@pytest.mark.parametrize("mode", [pytest.param("read", id="read"), pytest.param("write", id="write")])
@pytest.mark.asyncio
@XFAIL_WITHOUT_COROUTINE_CANCELLATION
async def test_acquire_cancellation_surfaces_acquire_and_rollback_errors(
    lock_file: str, mocker: MockerFixture, mode: Literal["read", "write"]
) -> None:
    acquire_started = asyncio.Event()
    finish_acquire = threading.Event()
    loop = asyncio.get_running_loop()
    acquire_error, rollback_error, prior_rollback_error = _patch_async_acquire_and_rollback_failure(
        mocker, loop, acquire_started, finish_acquire, mode
    )
    lock = AsyncReadWriteLock(lock_file, is_singleton=False)
    task = asyncio.create_task(getattr(lock, f"acquire_{mode}")())
    await acquire_started.wait()
    task.cancel("cancel acquire")
    finish_acquire.set()

    with pytest.raises(sqlite3.OperationalError, match="rollback failed") as info:
        await task
    assert info.value is rollback_error
    cancellation = rollback_error.__context__
    assert isinstance(cancellation, asyncio.CancelledError)
    assert (cancellation.args, cancellation.__context__, acquire_error.__context__) == (
        ("cancel acquire",),
        acquire_error,
        prior_rollback_error,
    )
    assert_read_write_lock_state(lock_file, OPPOSITE_MODE[mode], available=False)

    await getattr(lock, f"acquire_{mode}")()
    assert_read_write_lock_state(lock_file, OPPOSITE_MODE[mode], available=False)
    await lock.release()
    assert_read_write_lock_state(lock_file, OPPOSITE_MODE[mode], available=True)
    await lock.close()


def _patch_async_acquire_and_rollback_failure(
    mocker: MockerFixture,
    loop: asyncio.AbstractEventLoop,
    acquire_started: asyncio.Event,
    finish_acquire: threading.Event,
    mode: Literal["read", "write"],
) -> _RollbackFailures:
    acquire_error = sqlite3.OperationalError("acquire failed")
    acquire_error.sqlite_errorcode = sqlite3.SQLITE_ERROR
    rollback_error = sqlite3.OperationalError("rollback failed")
    rollback_error.sqlite_errorcode = sqlite3.SQLITE_ERROR
    prior_rollback_error = LookupError("prior rollback failure")
    rollback_error.__context__ = prior_rollback_error
    acquire_failed = False
    rollback_failed = False

    def execute(real_connection: sqlite3.Connection, statement: str) -> sqlite3.Cursor:
        nonlocal acquire_failed
        cursor = real_connection.execute(statement)
        if ("SELECT name" in statement if mode == "read" else "BEGIN EXCLUSIVE" in statement) and not acquire_failed:
            acquire_failed = True
            loop.call_soon_threadsafe(acquire_started.set)
            assert finish_acquire.wait(timeout=5)
            cursor.close()
            raise acquire_error
        return cursor

    def rollback(real_connection: sqlite3.Connection) -> None:
        nonlocal rollback_failed
        if not rollback_failed:
            rollback_failed = True
            try:
                raise prior_rollback_error
            except LookupError:
                raise rollback_error  # ruff:ignore[raise-without-from-inside-except]  # exercise implicit backend context preservation
        real_connection.rollback()

    patch_connection(mocker, execute=execute, rollback=rollback)
    return _RollbackFailures(acquire_error, rollback_error, prior_rollback_error)


class _RollbackFailures(NamedTuple):
    acquire_error: sqlite3.OperationalError
    rollback_error: sqlite3.OperationalError
    prior_rollback_error: LookupError
