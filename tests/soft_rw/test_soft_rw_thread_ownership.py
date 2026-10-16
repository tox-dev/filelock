from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING, Final

import pytest

from filelock import Timeout
from filelock._soft_rw import SoftReadWriteLock

if TYPE_CHECKING:
    from collections.abc import Generator
    from pathlib import Path

pytestmark = pytest.mark.requires_hard_links

# Bounds how long another thread may take to reach the lock, so a lock that never admits it fails instead of hanging.
_THREAD_WAIT: Final[float] = 10


@pytest.fixture
def lock_file(tmp_path: Path) -> str:
    return str(tmp_path / "test.lock")


@pytest.fixture
def lock(lock_file: str) -> Generator[SoftReadWriteLock]:
    instance = SoftReadWriteLock(
        lock_file, is_singleton=False, heartbeat_interval=0.1, stale_threshold=0.5, poll_interval=0.02
    )
    try:
        yield instance
    finally:
        instance.close()


@pytest.fixture
def other_thread() -> Generator[ThreadPoolExecutor]:
    with ThreadPoolExecutor(max_workers=1) as executor:
        yield executor


def _writer_gets_in(lock_file: str) -> bool:
    contender: Final = SoftReadWriteLock(lock_file, is_singleton=False, heartbeat_interval=0.1, stale_threshold=0.5)
    try:
        contender.acquire_write(blocking=False)
    except Timeout:
        return False
    finally:
        contender.close()
    return True


def test_soft_read_release_from_a_thread_without_a_read_level_raises(
    lock: SoftReadWriteLock, other_thread: ThreadPoolExecutor
) -> None:
    lock.acquire_read(timeout=2)

    with pytest.raises(RuntimeError, match="which does not hold it"):
        other_thread.submit(lock.release).result(_THREAD_WAIT)

    assert not _writer_gets_in(lock.lock_file)
    lock.release()


def test_soft_read_release_of_a_dropped_hold_leaves_a_newer_one_held(
    lock: SoftReadWriteLock, other_thread: ThreadPoolExecutor
) -> None:
    lock.acquire_read(timeout=2)
    lock.release(force=True)
    other_thread.submit(lock.acquire_read, 2).result(_THREAD_WAIT)

    with pytest.raises(RuntimeError, match="which does not hold it"):
        lock.release()

    assert not _writer_gets_in(lock.lock_file)
    other_thread.submit(lock.release).result(_THREAD_WAIT)


def test_soft_read_release_gives_back_only_the_callers_levels(
    lock: SoftReadWriteLock, other_thread: ThreadPoolExecutor
) -> None:
    lock.acquire_read(timeout=2)
    other_thread.submit(lock.acquire_read, 2).result(_THREAD_WAIT)
    lock.release()

    with pytest.raises(RuntimeError, match="which does not hold it"):
        lock.release()

    assert not _writer_gets_in(lock.lock_file)
    other_thread.submit(lock.release).result(_THREAD_WAIT)
    assert _writer_gets_in(lock.lock_file)
