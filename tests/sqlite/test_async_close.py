from __future__ import annotations

import asyncio
import threading
from typing import TYPE_CHECKING, Final, Literal

import pytest

from tests.read_write_helpers import (
    writer_gets_in,
)

pytest.importorskip("sqlite3")

import sqlite3

from filelock import AsyncReadWriteLock, ReadWriteLock
from tests.sqlite.async_helpers import TASK_WAIT, write_once

if TYPE_CHECKING:
    import functools

    from pytest_mock import MockerFixture

    from filelock import AcquireReturnProxy


pytestmark: Final[pytest.MarkDecorator] = pytest.mark.usefixtures("clear_sqlite_singletons")


@pytest.mark.asyncio
async def test_async_close_waits_out_an_acquire_in_flight_even_when_canceled(lock_file: str) -> None:
    holder: Final[ReadWriteLock] = ReadWriteLock(lock_file, is_singleton=False)
    holder.acquire_write()
    lock: Final[AsyncReadWriteLock] = AsyncReadWriteLock(lock_file, is_singleton=False)
    acquiring = asyncio.create_task(lock.acquire_write(timeout=TASK_WAIT))
    await asyncio.sleep(0)  # the acquire is now waiting in the backend
    closing = asyncio.create_task(lock.close())
    await asyncio.sleep(0)
    closing.cancel()
    await asyncio.sleep(0)
    holder.release()

    with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
        await acquiring
    with pytest.raises(asyncio.CancelledError):
        await closing

    assert writer_gets_in(lock_file)
    holder.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("is_singleton", [pytest.param(False, id="owned"), pytest.param(True, id="singleton")])
async def test_close_interrupts_backend_wait_while_peer_holds(lock_file: str, *, is_singleton: bool) -> None:
    holder = ReadWriteLock(lock_file, is_singleton=False)
    holder.acquire_write()
    lock = AsyncReadWriteLock(lock_file, is_singleton=is_singleton)
    acquiring = asyncio.create_task(lock.acquire_write())
    try:
        await asyncio.sleep(0)
        async with asyncio.timeout(2):
            await lock.close()
            with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
                await acquiring
        assert not writer_gets_in(lock_file)
    finally:
        holder.close()
        await asyncio.gather(acquiring, return_exceptions=True)
        await lock.close()


@pytest.mark.parametrize("is_singleton", [pytest.param(True, id="singleton"), pytest.param(False, id="owned")])
@pytest.mark.parametrize("mode", [pytest.param("read", id="read"), pytest.param("write", id="write")])
@pytest.mark.asyncio
async def test_async_acquire_waiting_in_backend_during_close_is_refused(
    lock_file: str, is_singleton: bool, mode: Literal["read", "write"], mocker: MockerFixture
) -> None:
    holder: Final[ReadWriteLock] = ReadWriteLock(lock_file, is_singleton=False)
    holder.acquire_write()
    lock: Final[AsyncReadWriteLock] = AsyncReadWriteLock(lock_file, is_singleton=is_singleton)
    granted, resume = asyncio.Event(), asyncio.Event()
    run_acquire = lock._run_acquire

    async def gated_acquire(acquire: functools.partial[AcquireReturnProxy]) -> None:
        await run_acquire(acquire)
        granted.set()
        await resume.wait()

    mocker.patch.object(lock, "_run_acquire", autospec=True, side_effect=gated_acquire)
    acquiring = asyncio.create_task(getattr(lock, f"acquire_{mode}")(timeout=TASK_WAIT))
    holder.release()
    await granted.wait()
    closing = asyncio.create_task(lock.close())
    await asyncio.sleep(0)
    resume.set()

    with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
        await acquiring
    await closing

    assert writer_gets_in(lock_file)
    holder.close()


@pytest.mark.asyncio
async def test_async_acquire_keeps_its_hold_when_close_fails(lock_file: str, mocker: MockerFixture) -> None:
    holder: Final[ReadWriteLock] = ReadWriteLock(lock_file, is_singleton=False)
    holder.acquire_write()
    lock: Final[AsyncReadWriteLock] = AsyncReadWriteLock(lock_file, is_singleton=False)
    granted, resume, entered, finish = (asyncio.Event() for _ in range(4))
    run_acquire = lock._run_acquire

    async def gated_acquire(acquire: functools.partial[AcquireReturnProxy]) -> None:
        await run_acquire(acquire)
        granted.set()
        await resume.wait()

    mocker.patch.object(lock, "_run_acquire", autospec=True, side_effect=gated_acquire)

    async def work() -> None:
        async with lock.write_lock(timeout=TASK_WAIT):
            entered.set()
            await finish.wait()

    acquiring = asyncio.create_task(work())
    holder.release()
    await granted.wait()
    mocker.patch.object(lock._lock, "close", autospec=True, side_effect=OSError("close failed"))
    closing = asyncio.create_task(lock.close())
    await asyncio.sleep(0)
    resume.set()

    with pytest.raises(OSError, match="close failed"):
        await closing
    await entered.wait()
    assert not writer_gets_in(lock_file)

    finish.set()
    await acquiring
    assert writer_gets_in(lock_file)
    mocker.stopall()
    await lock.close()
    holder.close()


@pytest.mark.parametrize(
    ("release_error", "expected_error", "available"),
    [
        pytest.param(None, asyncio.CancelledError, True, id="release-succeeds"),
        pytest.param(OSError("release failed"), OSError, False, id="release-fails"),
    ],
)
@pytest.mark.asyncio
async def test_async_cancelled_acquire_after_failed_close_reconciles_hold(
    lock_file: str,
    mocker: MockerFixture,
    release_error: OSError | None,
    expected_error: type[BaseException],
    available: bool,
) -> None:
    holder: Final[ReadWriteLock] = ReadWriteLock(lock_file, is_singleton=False)
    holder.acquire_write()
    lock: Final[AsyncReadWriteLock] = AsyncReadWriteLock(lock_file, is_singleton=False)
    granted, resume = asyncio.Event(), asyncio.Event()
    close_started, finish_close = threading.Event(), threading.Event()
    run_acquire = lock._run_acquire

    async def gated_acquire(acquire: functools.partial[AcquireReturnProxy]) -> None:
        await run_acquire(acquire)
        granted.set()
        await resume.wait()

    def fail_close() -> None:
        close_started.set()
        assert finish_close.wait(5)
        msg = "close failed"
        raise OSError(msg)

    mocker.patch.object(lock, "_leave_backend", autospec=True, wraps=lock._leave_backend, side_effect=release_error)
    mocker.patch.object(lock, "_run_acquire", autospec=True, side_effect=gated_acquire)
    acquiring = asyncio.create_task(lock.acquire_write(timeout=TASK_WAIT))
    holder.release()
    await granted.wait()
    mocker.patch.object(lock._lock, "close", autospec=True, side_effect=fail_close)
    closing = asyncio.create_task(lock.close())
    await asyncio.sleep(0)
    resume.set()
    assert await asyncio.to_thread(close_started.wait, 5)
    acquiring.cancel()
    finish_close.set()

    with pytest.raises(OSError, match="close failed"):
        await closing
    with pytest.raises(expected_error) as info:
        await acquiring
    assert isinstance(info.value.__context__, asyncio.CancelledError) is not available
    assert writer_gets_in(lock_file) is available
    mocker.stopall()
    await lock.close()
    assert writer_gets_in(lock_file)
    holder.close()


@pytest.mark.asyncio
async def test_async_cancelled_acquire_during_close_leaves_no_hold(lock_file: str, mocker: MockerFixture) -> None:
    holder: Final[ReadWriteLock] = ReadWriteLock(lock_file, is_singleton=False)
    holder.acquire_write()
    lock: Final[AsyncReadWriteLock] = AsyncReadWriteLock(lock_file, is_singleton=False)
    granted, resume = asyncio.Event(), asyncio.Event()
    close_started, finish_close = threading.Event(), threading.Event()
    run_acquire, close_backend = lock._run_acquire, lock._lock.close

    async def gated_acquire(acquire: functools.partial[AcquireReturnProxy]) -> None:
        await run_acquire(acquire)
        granted.set()
        await resume.wait()

    def paused_close() -> None:
        close_started.set()
        assert finish_close.wait(5)
        close_backend()

    mocker.patch.object(lock, "_run_acquire", autospec=True, side_effect=gated_acquire)
    acquiring = asyncio.create_task(lock.acquire_write(timeout=TASK_WAIT))
    holder.release()
    await granted.wait()
    mocker.patch.object(lock._lock, "close", autospec=True, side_effect=paused_close)
    closing = asyncio.create_task(lock.close())
    await asyncio.sleep(0)
    resume.set()
    assert await asyncio.to_thread(close_started.wait, 5)
    acquiring.cancel()
    finish_close.set()

    await closing
    with pytest.raises(asyncio.CancelledError):
        await acquiring
    assert writer_gets_in(lock_file)
    holder.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("held_mode", "waiting_mode"),
    [pytest.param("write", "read", id="reader"), pytest.param("read", "write", id="writer")],
)
async def test_async_close_wakes_its_table_waiter(
    lock_file: str, held_mode: Literal["read", "write"], waiting_mode: Literal["read", "write"]
) -> None:
    holder, closing = AsyncReadWriteLock(lock_file), AsyncReadWriteLock(lock_file)
    await getattr(holder, f"acquire_{held_mode}")()
    acquiring = asyncio.create_task(getattr(closing, f"acquire_{waiting_mode}")())
    try:
        assert not (await asyncio.wait((acquiring,), timeout=0.05))[0]

        await closing.close()

        assert acquiring in (await asyncio.wait((acquiring,), timeout=1))[0]
        with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
            await acquiring
    finally:
        await holder.close()
        await asyncio.gather(acquiring, return_exceptions=True)


@pytest.mark.asyncio
async def test_closing_first_writer_preserves_next_writer(lock_file: str) -> None:
    holder, first, second = (AsyncReadWriteLock(lock_file) for _ in range(3))
    await holder.acquire_read()
    first_wait = asyncio.create_task(write_once(first))
    second_wait = asyncio.create_task(write_once(second))
    try:
        await asyncio.sleep(0)
        await first.close()
        with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
            await first_wait
        assert not second_wait.done()
        await holder.release()
        await asyncio.wait_for(second_wait, TASK_WAIT)
        assert writer_gets_in(lock_file)
    finally:
        await holder.close()
        await first.close()
        await second.close()
        await asyncio.gather(first_wait, second_wait, return_exceptions=True)


@pytest.mark.asyncio
async def test_async_close_of_waiting_writer_unblocks_other_readers(lock_file: str) -> None:
    holder, closing, reader = (AsyncReadWriteLock(lock_file) for _ in range(3))
    await holder.acquire_read()
    acquiring_write = asyncio.create_task(closing.acquire_write())
    await asyncio.wait((acquiring_write,), timeout=0.05)
    acquiring_read = asyncio.create_task(reader.acquire_read())
    try:
        assert not acquiring_write.done()
        assert not (await asyncio.wait((acquiring_read,), timeout=0.05))[0]

        await closing.close()

        with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
            await asyncio.wait_for(acquiring_write, 1)
        await asyncio.wait_for(acquiring_read, 1)
        assert not writer_gets_in(lock_file)
    finally:
        await holder.close()
        await reader.close()
        await asyncio.gather(acquiring_write, acquiring_read, return_exceptions=True)


@pytest.mark.asyncio
async def test_async_failed_close_keeps_pending_acquire_waiting(lock_file: str, mocker: MockerFixture) -> None:
    lock = AsyncReadWriteLock(lock_file)
    await lock.acquire_read()
    acquiring = asyncio.create_task(lock.acquire_write())
    await asyncio.wait((acquiring,), timeout=0.05)
    release = mocker.patch.object(
        ReadWriteLock, "_release_level_of", autospec=True, side_effect=sqlite3.OperationalError("release failed")
    )
    try:
        try:
            with pytest.raises(sqlite3.OperationalError, match="release failed"):
                await lock.close()
        finally:
            mocker.stop(release)

        assert not (await asyncio.wait((acquiring,), timeout=0.05))[0]
        await lock.release()
        await asyncio.wait_for(acquiring, 1)
    finally:
        await lock.close()
        await asyncio.gather(acquiring, return_exceptions=True)


@pytest.mark.asyncio
async def test_async_close_that_fails_before_any_acquire_leaves_the_wrapper_closed(
    lock_file: str, mocker: MockerFixture
) -> None:
    lock: Final[AsyncReadWriteLock] = AsyncReadWriteLock(lock_file, is_singleton=False)
    mocker.patch.object(ReadWriteLock, "close", autospec=True, side_effect=sqlite3.OperationalError("close failed"))
    with pytest.raises(sqlite3.OperationalError, match="close failed"):
        await lock.close()

    with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
        await lock.acquire_read()
