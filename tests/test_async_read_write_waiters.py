from __future__ import annotations

import asyncio
import gc
from typing import TYPE_CHECKING, Final

import pytest

pytest.importorskip("sqlite3")

from filelock import AsyncReadWriteLock, Timeout

if TYPE_CHECKING:
    from pathlib import Path


def _live_asyncio_futures() -> int:
    gc.collect()
    return sum(isinstance(candidate, asyncio.Future) for candidate in gc.get_objects())


async def _give_up_reading(lock: AsyncReadWriteLock) -> None:
    with pytest.raises(Timeout):
        await lock.acquire_read(timeout=0.001)


@pytest.mark.asyncio
async def test_async_reads_giving_up_behind_a_writer_leave_nothing_behind(tmp_path: Path) -> None:
    lock: Final = AsyncReadWriteLock(tmp_path / "lock.db", is_singleton=False)
    await lock.acquire_write()
    await asyncio.create_task(_give_up_reading(lock))
    before: Final = _live_asyncio_futures()

    for _ in range(100):
        await asyncio.create_task(_give_up_reading(lock))

    assert _live_asyncio_futures() == before
    await lock.release()
    await lock.close()
