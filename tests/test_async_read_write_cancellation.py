from __future__ import annotations

import asyncio
import functools
import multiprocessing
import sqlite3
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING, Literal, cast

import pytest
from async_filelock_cancellation_helpers import assert_cancellation_message
from read_write_helpers import assert_read_write_lock_state

from filelock import AsyncReadWriteLock, ReadWriteLock

if TYPE_CHECKING:
    from collections.abc import Callable
    from multiprocessing.sharedctypes import Synchronized
    from multiprocessing.synchronize import Event as EventType
    from pathlib import Path

    from pytest_mock import MockerFixture


@pytest.fixture
def lock_file(tmp_path: Path) -> str:
    return str(tmp_path / "test_lock.db")


@pytest.mark.parametrize("mode", [pytest.param("read", id="read"), pytest.param("write", id="write")])
@pytest.mark.asyncio
async def test_acquire_cancellation_before_executor_start_rolls_back(
    lock_file: str, mocker: MockerFixture, mode: Literal["read", "write"]
) -> None:
    executor_started = threading.Event()
    release_executor = threading.Event()
    rollback_started = asyncio.Event()
    finish_rollback = threading.Event()
    loop = asyncio.get_running_loop()
    _patch_async_rollback(mocker, loop, rollback_started, finish_rollback)

    with ThreadPoolExecutor(max_workers=1) as executor:
        blocker = executor.submit(_block_executor, executor_started, release_executor)
        assert executor_started.wait(timeout=5)
        lock = AsyncReadWriteLock(lock_file, is_singleton=False, executor=executor)
        acquire = lock.acquire_read if mode == "read" else lock.acquire_write
        task = asyncio.create_task(acquire())
        await asyncio.sleep(0)
        task.cancel("first cancellation")
        release_executor.set()
        await rollback_started.wait()
        assert_read_write_lock_state(lock_file, "write" if mode == "read" else "read", available=False)
        task.cancel("second cancellation")
        finish_rollback.set()
        with pytest.raises(asyncio.CancelledError) as info:
            await task
        blocker.result(timeout=5)
        assert_cancellation_message(info.value, "first cancellation")
        assert_read_write_lock_state(lock_file, "write" if mode == "read" else "read", available=True)
        with pytest.raises(RuntimeError, match="not held"):
            await lock.release()
        await lock.close()


@pytest.mark.parametrize(
    "cancel_caller",
    [pytest.param(False, id="executor"), pytest.param(True, id="caller-and-executor")],
)
def test_executor_cancels_queued_acquire_without_compensation(lock_file: str, *, cancel_caller: bool) -> None:
    context = multiprocessing.get_context("spawn")
    canceled = context.Value("b", False)
    process = context.Process(target=_cancel_queued_read_write_acquire, args=(lock_file, cancel_caller, canceled))
    process.start()
    process.join(timeout=10)
    if process.is_alive():  # pragma: no cover - cleanup for a hung child after the assertion fails
        process.terminate()
        process.join(timeout=5)

    assert (process.exitcode, canceled.value) == (0, True)


@pytest.mark.asyncio
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
async def test_acquire_cancellation_surfaces_compensation_failure(lock_file: str, mocker: MockerFixture) -> None:
    executor_started = threading.Event()
    release_executor = threading.Event()
    rollback_started = asyncio.Event()
    finish_rollback = threading.Event()
    rollback_error = _patch_async_rollback_failure(
        mocker, asyncio.get_running_loop(), rollback_started, finish_rollback
    )

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

        await lock.release()
        assert_read_write_lock_state(lock_file, "read", available=True)
        await lock.close()


@pytest.mark.parametrize("mode", [pytest.param("read", id="read"), pytest.param("write", id="write")])
@pytest.mark.asyncio
async def test_acquire_cancellation_while_sqlite_waits_rolls_back(
    lock_file: str, mocker: MockerFixture, mode: Literal["read", "write"]
) -> None:
    context = multiprocessing.get_context("spawn")
    holder_acquired = context.Event()
    release_holder = context.Event()
    holder = context.Process(
        target=_hold_read_write_lock,
        args=(lock_file, "write" if mode == "read" else "read", holder_acquired, release_holder),
    )
    holder.start()
    try:
        assert holder_acquired.wait(timeout=5), "read-write lock holder did not acquire"
        execute_started = asyncio.Event()
        _patch_async_execute_signal(mocker, asyncio.get_running_loop(), execute_started)
        lock = AsyncReadWriteLock(lock_file, is_singleton=False)
        task = asyncio.create_task((lock.acquire_read if mode == "read" else lock.acquire_write)())
        await execute_started.wait()
        task.cancel("cancel blocked acquire")
        release_holder.set()
        with pytest.raises(asyncio.CancelledError) as info:
            await task
        assert_cancellation_message(info.value, "cancel blocked acquire")
        holder.join(timeout=5)
        assert not holder.is_alive(), "read-write lock holder did not exit"
        assert_read_write_lock_state(lock_file, "write" if mode == "read" else "read", available=True)
        with pytest.raises(RuntimeError, match="not held"):
            await lock.release()
        await lock.close()
    finally:
        release_holder.set()
        if holder.is_alive():  # pragma: no cover - cleanup for a hung child after the assertion fails
            holder.terminate()
            holder.join(timeout=5)


@pytest.mark.parametrize("mode", [pytest.param("read", id="read"), pytest.param("write", id="write")])
@pytest.mark.asyncio
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
    task = asyncio.create_task((lock.acquire_read if mode == "read" else lock.acquire_write)())
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
    assert_read_write_lock_state(lock_file, "write" if mode == "read" else "read", available=False)

    await (lock.acquire_read if mode == "read" else lock.acquire_write)()
    assert_read_write_lock_state(lock_file, "write" if mode == "read" else "read", available=False)
    await lock.release()
    assert_read_write_lock_state(lock_file, "write" if mode == "read" else "read", available=True)
    await lock.close()


@pytest.mark.asyncio
async def test_close_cancellation_shuts_down_owned_executor(lock_file: str, mocker: MockerFixture) -> None:
    rollback_started = asyncio.Event()
    finish_rollback = threading.Event()
    _patch_async_rollback(mocker, asyncio.get_running_loop(), rollback_started, finish_rollback)
    lock = AsyncReadWriteLock(lock_file, is_singleton=False)
    await lock.acquire_write()
    executor = lock.executor
    task = asyncio.create_task(lock.close())
    await rollback_started.wait()
    task.cancel("cancel close")
    finish_rollback.set()

    with pytest.raises(asyncio.CancelledError) as info:
        await task
    assert_cancellation_message(info.value, "cancel close")
    assert_read_write_lock_state(lock_file, "read", available=True)
    with pytest.raises(RuntimeError):
        executor.submit(int)


@pytest.mark.parametrize("operation", [pytest.param("release", id="release"), pytest.param("close", id="close")])
@pytest.mark.asyncio
async def test_cancellation_surfaces_rollback_error(
    lock_file: str, mocker: MockerFixture, operation: Literal["release", "close"]
) -> None:
    rollback_started = asyncio.Event()
    finish_rollback = threading.Event()
    rollback_error = _patch_async_rollback_failure(
        mocker, asyncio.get_running_loop(), rollback_started, finish_rollback
    )
    lock = AsyncReadWriteLock(lock_file, is_singleton=False)
    await lock.acquire_write()
    executor = lock.executor
    task = asyncio.create_task(lock.release() if operation == "release" else lock.close())
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

    if operation == "release":
        await lock.release()
    await lock.close()
    assert_read_write_lock_state(lock_file, "read", available=True)
    with pytest.raises(RuntimeError):
        executor.submit(int)


@pytest.mark.parametrize("mode", [pytest.param("read", id="read"), pytest.param("write", id="write")])
@pytest.mark.asyncio
async def test_context_cancellation_preserves_body_and_rollback_contexts(
    lock_file: str, mocker: MockerFixture, mode: Literal["read", "write"]
) -> None:
    rollback_started = asyncio.Event()
    finish_rollback = threading.Event()
    body_error = ValueError("body failed")
    prior_error = LookupError("prior rollback failure")
    rollback_error = _patch_async_rollback_failure(
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
    probe_mode: Literal["read", "write"] = "write" if mode == "read" else "read"
    assert_read_write_lock_state(lock_file, probe_mode, available=False)

    await lock.release()
    await lock.close()
    assert_read_write_lock_state(lock_file, probe_mode, available=True)


def _patch_async_rollback(
    mocker: MockerFixture,
    loop: asyncio.AbstractEventLoop,
    rollback_started: asyncio.Event,
    finish_rollback: threading.Event,
) -> None:
    def rollback(real_connection: sqlite3.Connection) -> None:
        loop.call_soon_threadsafe(rollback_started.set)
        assert finish_rollback.wait(timeout=5)
        real_connection.rollback()

    _patch_async_connection(mocker, rollback=rollback)


def _cancel_queued_read_write_acquire(lock_file: str, cancel_caller: bool, canceled: Synchronized[bool]) -> None:
    async def cancel_queued_acquire() -> None:
        executor_started = threading.Event()
        release_executor = threading.Event()
        executor = ThreadPoolExecutor(max_workers=1)
        blocker = executor.submit(_block_executor, executor_started, release_executor)
        assert executor_started.wait(timeout=5)
        lock = AsyncReadWriteLock(lock_file, is_singleton=False, executor=executor)
        task = asyncio.create_task(lock.acquire_write())
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
            if cancel_caller and sys.version_info >= (3, 11):
                canceled.value = isinstance(context, asyncio.CancelledError) and context.args == ("caller canceled",)
            else:
                canceled.value = True
        contender = ReadWriteLock(lock_file, is_singleton=False)
        with contender.write_lock(blocking=False):
            pass
        contender.close()

    asyncio.run(cancel_queued_acquire())


def _patch_async_execute_signal(
    mocker: MockerFixture,
    loop: asyncio.AbstractEventLoop,
    execute_started: asyncio.Event,
) -> None:
    def executescript(real_connection: sqlite3.Connection, statement: str) -> sqlite3.Cursor:
        if "PRAGMA journal_mode" in statement:
            loop.call_soon_threadsafe(execute_started.set)
        return real_connection.executescript(statement)

    _patch_async_connection(mocker, executescript=executescript)


def _patch_async_rollback_failure(
    mocker: MockerFixture,
    loop: asyncio.AbstractEventLoop,
    rollback_started: asyncio.Event,
    finish_rollback: threading.Event,
    *,
    prior_context: BaseException | None = None,
) -> sqlite3.OperationalError:
    rollback_error = sqlite3.OperationalError("rollback failed")
    rollback_error.__context__ = prior_context
    rollback_failed = False

    def rollback(real_connection: sqlite3.Connection) -> None:
        nonlocal rollback_failed
        if rollback_failed:
            real_connection.rollback()
            return
        rollback_failed = True
        loop.call_soon_threadsafe(rollback_started.set)
        assert finish_rollback.wait(timeout=5)
        raise rollback_error

    _patch_async_connection(mocker, rollback=rollback)
    return rollback_error


def _patch_async_acquire_and_rollback_failure(
    mocker: MockerFixture,
    loop: asyncio.AbstractEventLoop,
    acquire_started: asyncio.Event,
    finish_acquire: threading.Event,
    mode: Literal["read", "write"],
) -> tuple[sqlite3.OperationalError, sqlite3.OperationalError, LookupError]:
    acquire_error = sqlite3.OperationalError("acquire failed")
    rollback_error = sqlite3.OperationalError("rollback failed")
    prior_rollback_error = LookupError("prior rollback failure")
    rollback_error.__context__ = prior_rollback_error
    acquire_failed = False
    rollback_failed = False

    def executescript(real_connection: sqlite3.Connection, statement: str) -> sqlite3.Cursor:
        nonlocal acquire_failed
        cursor = real_connection.executescript(statement)
        target = "SELECT name" in statement if mode == "read" else "BEGIN EXCLUSIVE" in statement
        if target and not acquire_failed:
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

    _patch_async_connection(mocker, executescript=executescript, rollback=rollback)
    return acquire_error, rollback_error, prior_rollback_error


def _patch_async_connection(
    mocker: MockerFixture,
    *,
    executescript: Callable[[sqlite3.Connection, str], sqlite3.Cursor] | None = None,
    rollback: Callable[[sqlite3.Connection], None] | None = None,
) -> None:
    real_connect = sqlite3.connect
    connection_count = 0

    def connect(
        database: str,
        *,
        factory: type[sqlite3.Connection],
        timeout: float,
    ) -> sqlite3.Connection:
        nonlocal connection_count
        real_connection = real_connect(
            database,
            check_same_thread=False,
            factory=factory,
            cached_statements=0,
            timeout=timeout,
        )
        connection_count += 1
        if connection_count != 2:
            return real_connection
        configuration: dict[str, Callable[[str], sqlite3.Cursor] | Callable[[], None]] = {}
        if executescript is not None:
            configuration["executescript.side_effect"] = functools.partial(executescript, real_connection)
        if rollback is not None:
            configuration["rollback.side_effect"] = functools.partial(rollback, real_connection)
        connection = mocker.MagicMock(spec_set=type(real_connection), wraps=real_connection, **configuration)
        mocker.patch.object(
            type(connection),
            "in_transaction",
            new_callable=mocker.PropertyMock,
            create=True,
            side_effect=lambda: real_connection.in_transaction,
        )
        return cast("sqlite3.Connection", connection)

    mocker.patch("filelock._read_write._connect", side_effect=connect)


def _block_executor(executor_started: threading.Event, release_executor: threading.Event) -> None:
    executor_started.set()
    assert release_executor.wait(timeout=5)


def _hold_read_write_lock(
    lock_file: str,
    mode: Literal["read", "write"],
    acquired: EventType,
    release: EventType,
) -> None:
    lock = ReadWriteLock(lock_file, is_singleton=False)
    (lock.acquire_read if mode == "read" else lock.acquire_write)()
    acquired.set()
    try:
        assert release.wait(timeout=10)
    finally:
        lock.release()
        lock.close()
