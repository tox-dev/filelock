from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Final, Literal

import pytest

from tests.read_write_helpers import (
    Interrupt,
    interrupt_once,
    writer_gets_in,
)

pytest.importorskip("sqlite3")

import sqlite3

from filelock import AsyncReadWriteLock, ReadWriteLock
from tests.sqlite.async_helpers import TASK_WAIT

if TYPE_CHECKING:
    from collections.abc import Callable

    from pytest_mock import MockerFixture


pytestmark: Final[pytest.MarkDecorator] = pytest.mark.usefixtures("clear_sqlite_singletons")


@pytest.mark.asyncio
async def test_async_read_after_a_sync_close_of_the_shared_singleton_raises(lock_file: str) -> None:
    lock: Final[AsyncReadWriteLock] = AsyncReadWriteLock(lock_file)
    await lock.acquire_read()
    ReadWriteLock(lock_file).close()

    with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
        await asyncio.create_task(lock.acquire_read())
    with pytest.raises(RuntimeError, match="not held by this task"):
        await lock.release()
    await lock.close()


@pytest.mark.parametrize(
    "sync_end",
    [
        pytest.param(ReadWriteLock.close, id="close"),
        pytest.param(lambda sync: sync.release(force=True), id="release"),
    ],
)
@pytest.mark.asyncio
async def test_async_close_after_a_sync_end_of_the_shared_singleton_returns(
    lock_file: str, sync_end: Callable[[ReadWriteLock], object]
) -> None:
    lock: Final[AsyncReadWriteLock] = AsyncReadWriteLock(lock_file)
    await lock.acquire_read()
    sync: Final[ReadWriteLock] = ReadWriteLock(lock_file)
    sync_end(sync)

    await lock.close()

    assert writer_gets_in(lock_file)


@pytest.mark.asyncio
async def test_async_release_after_a_sync_forced_release_raises_not_held_by_this_task(lock_file: str) -> None:
    lock: Final[AsyncReadWriteLock] = AsyncReadWriteLock(lock_file)
    await lock.acquire_read()
    ReadWriteLock(lock_file).release(force=True)

    with pytest.raises(RuntimeError, match="not held by this task"):
        await lock.release()
    await lock.close()


@pytest.mark.asyncio
async def test_async_read_after_a_sync_forced_release_takes_the_lock_again(lock_file: str) -> None:
    lock: Final[AsyncReadWriteLock] = AsyncReadWriteLock(lock_file)
    await lock.acquire_read()
    ReadWriteLock(lock_file).release(force=True)

    writer_got_in = await asyncio.create_task(_read_and_probe(lock))

    assert not writer_got_in
    await lock.close()


async def _read_and_probe(lock: AsyncReadWriteLock) -> bool:
    async with lock.read_lock(timeout=TASK_WAIT):
        return writer_gets_in(lock.lock_file)


@pytest.mark.parametrize(
    "sync_end",
    [
        pytest.param(ReadWriteLock.close, id="close"),
        pytest.param(lambda sync: sync.release(force=True), id="release"),
    ],
)
@pytest.mark.asyncio
async def test_async_read_after_an_interrupted_sync_forced_release_takes_the_lock_again(
    lock_file: str, mocker: MockerFixture, sync_end: Callable[[ReadWriteLock], object]
) -> None:
    lock: Final[AsyncReadWriteLock] = AsyncReadWriteLock(lock_file)
    await lock.acquire_read()
    mocker.patch.object(
        ReadWriteLock, "_clear_lock_state", new=interrupt_once(ReadWriteLock._clear_lock_state, before=True)
    )
    sync: Final[ReadWriteLock] = ReadWriteLock(lock_file)
    with pytest.raises(Interrupt):
        sync_end(sync)

    writer_got_in = await asyncio.create_task(_read_and_probe(lock))

    assert not writer_got_in
    await lock.close()


@pytest.mark.asyncio
async def test_async_read_treats_a_level_whose_connection_closed_meanwhile_as_gone(
    lock_file: str, mocker: MockerFixture
) -> None:
    lock: Final[AsyncReadWriteLock] = AsyncReadWriteLock(lock_file)
    await lock.acquire_read()
    real_ended = ReadWriteLock._transaction_ended
    closed = False

    def close_between_checks(instance: ReadWriteLock) -> bool:
        nonlocal closed
        if not closed:
            closed = True
            raise sqlite3.ProgrammingError
        return real_ended(instance)

    mocker.patch.object(ReadWriteLock, "_transaction_ended", autospec=True, side_effect=close_between_checks)

    writer_got_in = await asyncio.create_task(_read_and_probe(lock))

    assert not writer_got_in
    with pytest.raises(RuntimeError, match="not held by this task"):
        await lock.release()
    await lock.close()


@pytest.mark.parametrize("is_singleton", [pytest.param(True, id="singleton"), pytest.param(False, id="owned")])
@pytest.mark.asyncio
async def test_async_read_arriving_during_close_is_refused(lock_file: str, is_singleton: bool) -> None:
    lock: Final[AsyncReadWriteLock] = AsyncReadWriteLock(lock_file, is_singleton=is_singleton)
    await lock.acquire_read()
    arriving = asyncio.create_task(_read_and_probe(lock))  # first runs while close() waits for its backend call

    await lock.close()

    with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
        await arriving


@pytest.mark.asyncio
async def test_async_read_through_another_wrapper_during_close_holds_the_lock(lock_file: str) -> None:
    closing, other = AsyncReadWriteLock(lock_file), AsyncReadWriteLock(lock_file)
    await closing.acquire_read()
    arriving = asyncio.create_task(_read_and_probe(other))

    await closing.close()

    assert not await arriving
    await other.close()


@pytest.mark.parametrize("mode", [pytest.param("read", id="read"), pytest.param("write", id="write")])
@pytest.mark.asyncio
async def test_async_close_keeps_the_hold_taken_through_another_wrapper(
    lock_file: str, mode: Literal["read", "write"]
) -> None:
    holder, closing = AsyncReadWriteLock(lock_file), AsyncReadWriteLock(lock_file)
    await getattr(holder, f"acquire_{mode}")()

    await asyncio.create_task(closing.close())

    assert not writer_gets_in(lock_file)
    await holder.release()
    assert writer_gets_in(lock_file)
    await holder.close()


@pytest.mark.asyncio
async def test_async_close_of_the_entering_wrapper_keeps_a_level_taken_through_another(lock_file: str) -> None:
    entering, nested = AsyncReadWriteLock(lock_file), AsyncReadWriteLock(lock_file)
    await entering.acquire_read()
    await nested.acquire_read()

    await entering.close()

    assert not writer_gets_in(lock_file)
    await nested.release()
    assert writer_gets_in(lock_file)
    await nested.close()


@pytest.mark.asyncio
async def test_async_close_of_the_nested_wrapper_ends_only_its_own_level(lock_file: str) -> None:
    entering, nested = AsyncReadWriteLock(lock_file), AsyncReadWriteLock(lock_file)
    await entering.acquire_read()
    await nested.acquire_read()

    await nested.close()

    await entering.release()
    assert writer_gets_in(lock_file)
    await entering.close()


@pytest.mark.asyncio
async def test_async_release_through_each_wrapper_ends_the_hold_after_the_last(lock_file: str) -> None:
    entering, nested = AsyncReadWriteLock(lock_file), AsyncReadWriteLock(lock_file)
    await entering.acquire_read()
    await nested.acquire_read()

    await nested.release()

    assert not writer_gets_in(lock_file)
    await entering.release()
    assert writer_gets_in(lock_file)
    await entering.close()
    await nested.close()


@pytest.mark.asyncio
async def test_async_forced_release_through_a_wrapper_without_a_level_releases_nothing(lock_file: str) -> None:
    holder, other = AsyncReadWriteLock(lock_file), AsyncReadWriteLock(lock_file)
    await holder.acquire_read()

    await other.release(force=True)

    assert not writer_gets_in(lock_file)
    await holder.release()
    assert writer_gets_in(lock_file)
    await holder.close()
    await other.close()


@pytest.mark.asyncio
async def test_async_release_through_a_wrapper_without_a_level_raises(lock_file: str) -> None:
    holder, other = AsyncReadWriteLock(lock_file), AsyncReadWriteLock(lock_file)
    await holder.acquire_read()

    with pytest.raises(RuntimeError, match="through this wrapper"):
        await other.release()

    assert not writer_gets_in(lock_file)
    await holder.release()
    await holder.close()
    await other.close()


@pytest.mark.asyncio
async def test_async_close_keeps_a_sync_reader_of_the_shared_singleton(lock_file: str) -> None:
    lock: Final[AsyncReadWriteLock] = AsyncReadWriteLock(lock_file)
    with ReadWriteLock(lock_file).read_lock():
        async with lock.read_lock():
            pass
        await lock.close()

        assert not writer_gets_in(lock_file)


@pytest.mark.asyncio
async def test_async_close_of_a_singleton_wrapper_ends_the_holds_taken_through_it(lock_file: str) -> None:
    lock: Final[AsyncReadWriteLock] = AsyncReadWriteLock(lock_file)
    await lock.acquire_write()

    await lock.close()

    assert writer_gets_in(lock_file)
    with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
        await lock.acquire_read()
