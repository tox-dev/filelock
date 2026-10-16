from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING, Final, Literal

import pytest

pytest.importorskip("sqlite3")

from filelock import AsyncReadWriteLock, ReadWriteLock
from filelock._soft_rw import AsyncSoftReadWriteLock, SoftReadWriteLock

if TYPE_CHECKING:
    from collections.abc import Callable, Generator
    from pathlib import Path


@pytest.fixture(autouse=True)
def _clear_singleton_caches() -> Generator[None]:
    ReadWriteLock._instances.clear()
    SoftReadWriteLock._instances.clear()
    yield
    for lock in filter(None, (ref() for ref in list(ReadWriteLock._instances.valuerefs()))):
        lock.close()
    for soft in filter(None, (ref() for ref in list(SoftReadWriteLock._instances.valuerefs()))):
        soft.close()
    ReadWriteLock._instances.clear()
    SoftReadWriteLock._instances.clear()


@pytest.fixture
def worker() -> Generator[ThreadPoolExecutor]:
    with ThreadPoolExecutor(max_workers=1) as executor:
        yield executor


def _sqlite(path: Path, worker: ThreadPoolExecutor) -> tuple[AsyncReadWriteLock, ReadWriteLock]:
    return AsyncReadWriteLock(path / "lock.db", executor=worker), ReadWriteLock(path / "lock.db")


def _soft(  # pragma: needs hard-link
    path: Path, worker: ThreadPoolExecutor
) -> tuple[AsyncSoftReadWriteLock, SoftReadWriteLock]:
    return AsyncSoftReadWriteLock(path / "lock", executor=worker), SoftReadWriteLock(path / "lock")


_FAMILIES: Final = pytest.mark.parametrize(
    "make",
    [pytest.param(_sqlite, id="sqlite"), pytest.param(_soft, id="soft", marks=pytest.mark.requires_hard_links)],
)


@_FAMILIES
@pytest.mark.asyncio
async def test_async_write_hold_refuses_a_sync_writer_on_the_entering_worker(
    tmp_path: Path,
    worker: ThreadPoolExecutor,
    make: Callable[
        [Path, ThreadPoolExecutor],
        tuple[AsyncReadWriteLock | AsyncSoftReadWriteLock, ReadWriteLock | SoftReadWriteLock],
    ],
) -> None:
    lock, sync = make(tmp_path, worker)
    await lock.acquire_write()

    with pytest.raises(RuntimeError, match="while it is held by thread"):
        await asyncio.get_running_loop().run_in_executor(worker, lambda: sync.acquire_write(blocking=False))
    await lock.release()
    await lock.close()


@_FAMILIES
@pytest.mark.parametrize("mode", [pytest.param("read", id="read"), pytest.param("write", id="write")])
@pytest.mark.asyncio
async def test_async_hold_refuses_a_sync_release_on_the_entering_worker(
    tmp_path: Path,
    worker: ThreadPoolExecutor,
    make: Callable[
        [Path, ThreadPoolExecutor],
        tuple[AsyncReadWriteLock | AsyncSoftReadWriteLock, ReadWriteLock | SoftReadWriteLock],
    ],
    mode: Literal["read", "write"],
) -> None:
    lock, sync = make(tmp_path, worker)
    await (lock.acquire_read() if mode == "read" else lock.acquire_write())

    with pytest.raises(RuntimeError, match="Cannot release"):
        await asyncio.get_running_loop().run_in_executor(worker, sync.release)
    await lock.release()
    await lock.close()


@_FAMILIES
@pytest.mark.asyncio
async def test_async_write_acquire_on_a_worker_holding_the_sync_write_lock_raises(
    tmp_path: Path,
    worker: ThreadPoolExecutor,
    make: Callable[
        [Path, ThreadPoolExecutor],
        tuple[AsyncReadWriteLock | AsyncSoftReadWriteLock, ReadWriteLock | SoftReadWriteLock],
    ],
) -> None:
    lock, sync = make(tmp_path, worker)
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(worker, sync.acquire_write)

    with pytest.raises(RuntimeError, match="while it is held by thread"):
        await lock.acquire_write(blocking=False)
    await loop.run_in_executor(worker, sync.release)
    await lock.close()
