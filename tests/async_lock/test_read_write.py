from __future__ import annotations

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING, Final, Literal

import pytest

pytest.importorskip("sqlite3")

from filelock import AsyncReadWriteLock, AsyncSoftReadWriteLock, ReadWriteLock, SoftReadWriteLock, Timeout
from tests.async_lock.cancellation import FailingSubmitExecutor

if TYPE_CHECKING:
    from collections.abc import Callable, Generator
    from pathlib import Path


pytestmark: Final[pytest.MarkDecorator] = pytest.mark.usefixtures("clear_rw_singletons")


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


_FAMILIES: Final[pytest.MarkDecorator] = pytest.mark.parametrize(
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
    await getattr(lock, f"acquire_{mode}")()

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


@_FAMILIES
@pytest.mark.asyncio
async def test_async_release_whose_submission_failed_never_runs(
    tmp_path: Path,
    make: Callable[
        [Path, ThreadPoolExecutor],
        tuple[AsyncReadWriteLock | AsyncSoftReadWriteLock, ReadWriteLock | SoftReadWriteLock],
    ],
) -> None:
    with FailingSubmitExecutor(after_call_ran=False) as executor:
        lock, sync = make(tmp_path, executor)
        await lock.acquire_write()
        finish_blocker = threading.Event()
        blocker = executor.submit(finish_blocker.wait, 5)
        executor.fail_next_submit = True

        with pytest.raises(RuntimeError, match="can't start new thread"):
            await lock.release()
        finish_blocker.set()
        assert blocker.result(timeout=5)
        # The pool has one worker, so it took the call queued behind the blocker before it ran this one.
        executor.submit(threading.get_ident).result(timeout=5)

        contender = type(sync)(sync.lock_file, is_singleton=False)
        with pytest.raises(Timeout):
            contender.acquire_read(blocking=False)
        contender.close()
        await lock.release()
        await lock.close()
