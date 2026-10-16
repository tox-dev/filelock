from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Final

import pytest

from filelock import Timeout
from filelock._soft_rw import AsyncSoftReadWriteLock, SoftReadWriteLock
from tests.capability_marks import XFAIL_WITHOUT_COROUTINE_CANCELLATION

if TYPE_CHECKING:
    from collections.abc import Generator
    from pathlib import Path

pytestmark = pytest.mark.requires_hard_links


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


def _assert_writer_kept_out(lock_file: str) -> None:
    contender: Final = SoftReadWriteLock(lock_file, is_singleton=False, heartbeat_interval=0.1, stale_threshold=0.5)
    try:
        with pytest.raises(Timeout):
            contender.acquire_write(blocking=False)
    finally:
        contender.close()


@pytest.mark.asyncio
async def test_async_soft_release_leaves_a_sync_reader_of_the_same_singleton_holding(lock_file: str) -> None:
    async_lock: Final = AsyncSoftReadWriteLock(lock_file)
    with SoftReadWriteLock(lock_file).read_lock():
        async with async_lock.read_lock(timeout=5):
            pass

        await asyncio.to_thread(_assert_writer_kept_out, lock_file)
    await async_lock.close()


@pytest.mark.asyncio
@XFAIL_WITHOUT_COROUTINE_CANCELLATION
async def test_async_soft_canceled_acquire_leaves_a_sync_reader_of_the_same_singleton_holding(lock_file: str) -> None:
    async_lock: Final = AsyncSoftReadWriteLock(lock_file)
    await asyncio.to_thread(lambda: None)  # starts the executor worker, so the acquire below is in flight when canceled
    with SoftReadWriteLock(lock_file).read_lock():
        acquiring = asyncio.create_task(async_lock.acquire_read(timeout=5))
        await asyncio.sleep(0)
        acquiring.cancel()
        with pytest.raises(asyncio.CancelledError):
            await acquiring

        await asyncio.to_thread(_assert_writer_kept_out, lock_file)
    await async_lock.close()
