from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Final, Literal

import pytest

from filelock import Timeout
from filelock._soft_rw import AsyncSoftReadWriteLock, SoftReadWriteLock

if TYPE_CHECKING:
    from collections.abc import Generator
    from pathlib import Path

pytestmark = pytest.mark.requires_hard_links

# Bounds every wait on another task, so a lock that never admits it fails the test instead of hanging the suite.
_TASK_WAIT: Final[float] = 10


@pytest.fixture(autouse=True)
def _clear_singletons() -> Generator[None]:
    SoftReadWriteLock._instances.clear()
    yield
    for lock in filter(None, (ref() for ref in list(SoftReadWriteLock._instances.valuerefs()))):
        lock.close()
    SoftReadWriteLock._instances.clear()


@pytest.fixture
def lock_file(tmp_path: Path) -> str:
    return str(tmp_path / "foo.lock")


def _make(lock_file: str, *, is_singleton: bool = True) -> AsyncSoftReadWriteLock:
    return AsyncSoftReadWriteLock(
        lock_file, is_singleton=is_singleton, heartbeat_interval=0.1, stale_threshold=0.5, poll_interval=0.02
    )


def _writer_gets_in(lock_file: str) -> bool:
    contender: Final = SoftReadWriteLock(lock_file, is_singleton=False, heartbeat_interval=0.1, stale_threshold=0.5)
    try:
        contender.acquire_write(blocking=False)
    except Timeout:
        return False
    finally:
        contender.close()
    return True


async def _read_and_probe(lock: AsyncSoftReadWriteLock) -> bool:
    async with lock.read_lock(timeout=_TASK_WAIT):
        return await asyncio.to_thread(_writer_gets_in, lock.lock_file)


@pytest.mark.asyncio
async def test_async_soft_read_after_a_sync_close_of_the_shared_singleton_raises(lock_file: str) -> None:
    lock: Final = _make(lock_file)
    await lock.acquire_read(timeout=_TASK_WAIT)
    await asyncio.to_thread(SoftReadWriteLock(lock_file).close)

    with pytest.raises(RuntimeError, match="has been closed"):
        await asyncio.create_task(lock.acquire_read(timeout=_TASK_WAIT))
    with pytest.raises(RuntimeError, match="not held by this task"):
        await lock.release()
    await lock.close()


@pytest.mark.parametrize("is_singleton", [pytest.param(True, id="singleton"), pytest.param(False, id="owned")])
@pytest.mark.asyncio
async def test_async_soft_read_arriving_during_close_is_refused(lock_file: str, is_singleton: bool) -> None:
    lock: Final = _make(lock_file, is_singleton=is_singleton)
    await lock.acquire_read(timeout=_TASK_WAIT)
    arriving = asyncio.create_task(_read_and_probe(lock))  # first runs while close() waits for its backend call

    await lock.close()

    with pytest.raises(RuntimeError, match="has been closed"):
        await arriving


@pytest.mark.asyncio
async def test_async_soft_read_through_another_wrapper_during_close_holds_the_lock(lock_file: str) -> None:
    closing, other = _make(lock_file), _make(lock_file)
    await closing.acquire_read(timeout=_TASK_WAIT)
    arriving = asyncio.create_task(_read_and_probe(other))

    await closing.close()

    assert not await arriving
    await other.close()


@pytest.mark.parametrize("mode", [pytest.param("read", id="read"), pytest.param("write", id="write")])
@pytest.mark.asyncio
async def test_async_soft_close_keeps_the_hold_taken_through_another_wrapper(
    lock_file: str, mode: Literal["read", "write"]
) -> None:
    holder, closing = _make(lock_file), _make(lock_file)
    await (holder.acquire_read if mode == "read" else holder.acquire_write)(timeout=_TASK_WAIT)

    await asyncio.create_task(closing.close())

    assert not await asyncio.to_thread(_writer_gets_in, lock_file)
    await holder.release()
    assert await asyncio.to_thread(_writer_gets_in, lock_file)
    await holder.close()


@pytest.mark.asyncio
async def test_async_soft_close_of_a_singleton_wrapper_ends_the_holds_taken_through_it(lock_file: str) -> None:
    lock: Final = _make(lock_file)
    await lock.acquire_write(timeout=_TASK_WAIT)

    await lock.close()
    await lock.close()

    assert await asyncio.to_thread(_writer_gets_in, lock_file)
    with pytest.raises(RuntimeError, match="has been closed"):
        await lock.acquire_read(timeout=_TASK_WAIT)
