from __future__ import annotations

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING, Final

import pytest

from filelock import AsyncSoftReadWriteLock, Timeout
from filelock._soft_rw import SoftReadWriteLock
from filelock._soft_rw._protocol import GenerationLog, Snapshot
from filelock._soft_rw._storage import OsFiles

if TYPE_CHECKING:
    from collections.abc import Generator
    from pathlib import Path

    from filelock._lease import LeaseCompromise

pytestmark = pytest.mark.requires_hard_links

# Bounds the wait for the heartbeat to notice the loss, so a lock that never reports it fails instead of hanging.
_HEARTBEAT_WAIT: Final[float] = 10


@pytest.fixture
def lock_file(tmp_path: Path) -> str:
    return str(tmp_path / "test.lock")


@pytest.fixture
def lost() -> threading.Event:
    return threading.Event()


@pytest.fixture
def lock(lock_file: str, lost: threading.Event) -> Generator[SoftReadWriteLock]:
    def report(_compromise: LeaseCompromise) -> None:
        lost.set()

    instance = SoftReadWriteLock(
        lock_file,
        is_singleton=False,
        heartbeat_interval=0.05,
        stale_threshold=0.2,
        poll_interval=0.02,
        on_compromise=report,
    )
    try:
        yield instance
    finally:
        instance.close()


def _evict_every_holder(lock_file: str) -> None:
    log = GenerationLog(OsFiles(lock_file), lock_file, f"{lock_file}.rw")
    assert log.commit(Snapshot(generation=log.latest().generation + 1, writer=None, readers=frozenset()))


def test_soft_read_on_a_lost_hold_raises(lock: SoftReadWriteLock, lost: threading.Event) -> None:
    lock.acquire_read(timeout=2)
    _evict_every_holder(lock.lock_file)
    assert lost.wait(_HEARTBEAT_WAIT)

    with ThreadPoolExecutor(max_workers=1) as other_thread, pytest.raises(RuntimeError, match="hold was lost"):
        other_thread.submit(lock.acquire_read, 1).result(_HEARTBEAT_WAIT)
    lock.release()


def test_soft_read_after_the_lost_hold_drains_asks_the_log(lock: SoftReadWriteLock, lost: threading.Event) -> None:
    lock.acquire_read(timeout=2)
    _evict_every_holder(lock.lock_file)
    assert lost.wait(_HEARTBEAT_WAIT)
    lock.release()
    writer: Final = SoftReadWriteLock(
        lock.lock_file, is_singleton=False, heartbeat_interval=0.05, stale_threshold=0.2, poll_interval=0.02
    )
    writer.acquire_write(timeout=2)

    with pytest.raises(Timeout):
        lock.acquire_read(blocking=False)
    writer.close()


@pytest.mark.asyncio
async def test_async_soft_read_joining_a_lost_hold_raises(lock_file: str) -> None:
    lost = threading.Event()
    lock: Final = AsyncSoftReadWriteLock(
        lock_file,
        is_singleton=False,
        heartbeat_interval=0.05,
        stale_threshold=0.2,
        poll_interval=0.02,
        on_compromise=lambda _compromise: lost.set(),
    )
    await lock.acquire_read(timeout=2)
    _evict_every_holder(lock_file)
    assert await asyncio.to_thread(lost.wait, _HEARTBEAT_WAIT)

    with pytest.raises(RuntimeError, match="hold was lost"):
        await asyncio.create_task(lock.acquire_read(timeout=1))
    await lock.release()
    await lock.close()
