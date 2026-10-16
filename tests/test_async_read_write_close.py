from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Final, Literal

import pytest

pytest.importorskip("sqlite3")

import sqlite3

from filelock import AsyncReadWriteLock, ReadWriteLock, Timeout

if TYPE_CHECKING:
    from collections.abc import Generator
    from pathlib import Path

    from pytest_mock import MockerFixture

# Bounds every wait on another task, so a lock that never admits it fails the test instead of hanging the suite.
_TASK_WAIT: Final[float] = 10


@pytest.fixture(autouse=True)
def _clear_singleton_cache() -> Generator[None]:
    ReadWriteLock._instances.clear()
    yield
    for ref in list(ReadWriteLock._instances.valuerefs()):
        if (lock := ref()) is not None:  # pragma: no cover  # cache is normally emptied before teardown
            lock.close()
    ReadWriteLock._instances.clear()


@pytest.fixture
def lock_file(tmp_path: Path) -> str:
    return str(tmp_path / "test_lock.db")


def _writer_gets_in(lock_file: str) -> bool:
    contender: Final = ReadWriteLock(lock_file, is_singleton=False)
    try:
        contender.acquire_write(blocking=False)
    except Timeout:
        return False
    finally:
        contender.close()
    return True


async def _read_and_probe(lock: AsyncReadWriteLock) -> bool:
    async with lock.read_lock(timeout=_TASK_WAIT):
        return _writer_gets_in(lock.lock_file)


@pytest.mark.asyncio
async def test_async_read_after_a_sync_close_of_the_shared_singleton_raises(lock_file: str) -> None:
    lock: Final = AsyncReadWriteLock(lock_file)
    await lock.acquire_read()
    ReadWriteLock(lock_file).close()

    with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
        await asyncio.create_task(lock.acquire_read())
    with pytest.raises(RuntimeError, match="not held by this task"):
        await lock.release()
    await lock.close()


@pytest.mark.asyncio
async def test_async_read_after_a_sync_forced_release_takes_the_lock_again(lock_file: str) -> None:
    lock: Final = AsyncReadWriteLock(lock_file)
    await lock.acquire_read()
    ReadWriteLock(lock_file).release(force=True)

    writer_got_in = await asyncio.create_task(_read_and_probe(lock))

    assert not writer_got_in
    await lock.close()


@pytest.mark.parametrize("is_singleton", [pytest.param(True, id="singleton"), pytest.param(False, id="owned")])
@pytest.mark.asyncio
async def test_async_read_arriving_during_close_is_refused(lock_file: str, is_singleton: bool) -> None:
    lock: Final = AsyncReadWriteLock(lock_file, is_singleton=is_singleton)
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
    await (holder.acquire_read() if mode == "read" else holder.acquire_write())

    await asyncio.create_task(closing.close())

    assert not _writer_gets_in(lock_file)
    await holder.release()
    assert _writer_gets_in(lock_file)
    await holder.close()


@pytest.mark.asyncio
async def test_async_close_keeps_a_sync_reader_of_the_shared_singleton(lock_file: str) -> None:
    lock: Final = AsyncReadWriteLock(lock_file)
    with ReadWriteLock(lock_file).read_lock():
        async with lock.read_lock():
            pass
        await lock.close()

        assert not _writer_gets_in(lock_file)


@pytest.mark.asyncio
async def test_async_close_of_a_singleton_wrapper_ends_the_holds_taken_through_it(lock_file: str) -> None:
    lock: Final = AsyncReadWriteLock(lock_file)
    await lock.acquire_write()

    await lock.close()

    assert _writer_gets_in(lock_file)
    with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
        await lock.acquire_read()


@pytest.mark.asyncio
async def test_async_close_waits_out_an_acquire_in_flight_even_when_canceled(lock_file: str) -> None:
    holder: Final = ReadWriteLock(lock_file, is_singleton=False)
    holder.acquire_write()
    lock: Final = AsyncReadWriteLock(lock_file, is_singleton=False)
    acquiring = asyncio.create_task(lock.acquire_write(timeout=_TASK_WAIT))
    await asyncio.sleep(0)  # the acquire is now waiting in the backend
    closing = asyncio.create_task(lock.close())
    await asyncio.sleep(0)
    closing.cancel()
    await asyncio.sleep(0)
    holder.release()

    await acquiring
    with pytest.raises(asyncio.CancelledError):
        await closing

    assert _writer_gets_in(lock_file)
    holder.close()


@pytest.mark.asyncio
async def test_async_close_that_fails_before_any_acquire_leaves_the_wrapper_closed(
    lock_file: str, mocker: MockerFixture
) -> None:
    lock: Final = AsyncReadWriteLock(lock_file, is_singleton=False)
    mocker.patch.object(ReadWriteLock, "close", autospec=True, side_effect=sqlite3.OperationalError("close failed"))
    with pytest.raises(sqlite3.OperationalError, match="close failed"):
        await lock.close()

    with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
        await lock.acquire_read()
