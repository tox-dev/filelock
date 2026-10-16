from __future__ import annotations

import asyncio
import threading
import time
from typing import TYPE_CHECKING, Final, Literal

import pytest

pytest.importorskip("sqlite3")

from collections.abc import Callable

from filelock import AsyncReadWriteLock, ReadWriteLock, Timeout
from filelock._soft_rw import AsyncSoftReadWriteLock, SoftReadWriteLock
from tests.capability_marks import XFAIL_WITHOUT_COROUTINE_CANCELLATION

if TYPE_CHECKING:
    from collections.abc import Generator
    from pathlib import Path

    from pytest_mock import MockerFixture

# The other process's hold ends this long after the wait starts, so a wait that ignores its deadline ends then.
_HOLD: Final[float] = 3
# The caller's own deadline, far below _HOLD.
_DEADLINE: Final[float] = 0.2

pytestmark = XFAIL_WITHOUT_COROUTINE_CANCELLATION


@pytest.fixture(autouse=True)
def _clear_singleton_caches() -> Generator[None]:
    caches: Final = (ReadWriteLock._instances, SoftReadWriteLock._instances)
    for cache in caches:
        cache.clear()
    yield
    # One loop for both caches, so a platform where only one kind of lock runs still closes through it.
    for lock in filter(None, [ref() for cache in caches for ref in list(cache.valuerefs())]):
        lock.close()
    for cache in caches:
        cache.clear()


def _sqlite(path: Path, *, is_singleton: bool) -> tuple[ReadWriteLock, AsyncReadWriteLock]:
    return ReadWriteLock(path / "lock.db", is_singleton=is_singleton), AsyncReadWriteLock(path / "lock.db")


def _soft(  # pragma: needs hard-link
    path: Path, *, is_singleton: bool
) -> tuple[SoftReadWriteLock, AsyncSoftReadWriteLock]:
    return (
        SoftReadWriteLock(
            path / "lock", is_singleton=is_singleton, heartbeat_interval=0.1, stale_threshold=0.5, poll_interval=0.02
        ),
        AsyncSoftReadWriteLock(path / "lock", heartbeat_interval=0.1, stale_threshold=0.5, poll_interval=0.02),
    )


_Make = Callable[..., tuple[ReadWriteLock | SoftReadWriteLock, AsyncReadWriteLock | AsyncSoftReadWriteLock]]
_FAMILIES: Final = pytest.mark.parametrize(
    "make", [pytest.param(_sqlite, id="sqlite"), pytest.param(_soft, id="soft", marks=pytest.mark.requires_hard_links)]
)
_MODES: Final = pytest.mark.parametrize("mode", [pytest.param("read", id="read"), pytest.param("write", id="write")])


async def _acquire_within_deadline(
    lock: AsyncReadWriteLock | AsyncSoftReadWriteLock, mode: Literal["read", "write"], timeout: float
) -> float:
    start = time.perf_counter()
    with pytest.raises(TimeoutError) as info:
        async with asyncio.timeout(_DEADLINE):
            await (lock.acquire_read if mode == "read" else lock.acquire_write)(timeout)
    assert not isinstance(info.value, Timeout)
    return time.perf_counter() - start


@_FAMILIES
@_MODES
@pytest.mark.parametrize(
    "timeout", [pytest.param(-1, id="no-lock-timeout"), pytest.param(_HOLD * 2, id="lock-timeout")]
)
@pytest.mark.asyncio
async def test_async_acquire_behind_another_process_honors_the_callers_deadline(
    tmp_path: Path, make: _Make, mode: Literal["read", "write"], timeout: float
) -> None:
    holder, lock = make(tmp_path, is_singleton=False)
    holder.acquire_write()
    ending = asyncio.get_running_loop().call_later(_HOLD, holder.release)

    elapsed = await _acquire_within_deadline(lock, mode, timeout)

    assert elapsed < _HOLD
    ending.cancel()
    holder.release()
    await (lock.acquire_read if mode == "read" else lock.acquire_write)(_HOLD * 2)
    await lock.release()
    await lock.close()


@_FAMILIES
@pytest.mark.asyncio
async def test_async_acquire_behind_a_sync_acquire_of_the_same_instance_honors_the_callers_deadline(
    tmp_path: Path, make: _Make
) -> None:
    holder, lock = make(tmp_path, is_singleton=False)
    shared, _ = make(tmp_path, is_singleton=True)
    holder.acquire_write()

    # A thread waiting in the shared instance's acquire keeps that instance busy for the task too.
    def write_once() -> None:
        with shared.write_lock(timeout=_HOLD * 2):
            pass

    waiting = threading.Thread(target=write_once)
    waiting.start()
    ending = asyncio.get_running_loop().call_later(_HOLD, holder.release)

    elapsed = await _acquire_within_deadline(lock, "write", -1)

    assert elapsed < _HOLD
    ending.cancel()
    holder.release()
    await asyncio.to_thread(waiting.join, _HOLD * 2)
    await lock.acquire_write(timeout=_HOLD)
    await lock.release()
    await lock.close()


@_FAMILIES
@pytest.mark.asyncio
async def test_async_acquire_behind_a_sync_acquire_of_the_same_instance_times_out_at_its_own_deadline(
    tmp_path: Path, make: _Make, mocker: MockerFixture
) -> None:
    # With one thread alive the wait is not sliced, so the thread lock is polled across several intervals.
    mocker.patch.object(threading, "active_count", return_value=1)
    holder, lock = make(tmp_path, is_singleton=False)
    shared, _ = make(tmp_path, is_singleton=True)
    holder.acquire_write()

    def write_once() -> None:
        with shared.write_lock(timeout=_HOLD * 2):
            pass

    waiting = threading.Thread(target=write_once)
    waiting.start()
    ending = asyncio.get_running_loop().call_later(_HOLD, holder.release)
    start = time.perf_counter()

    with pytest.raises(Timeout):
        await lock.acquire_write(_DEADLINE)

    assert time.perf_counter() - start < _HOLD
    ending.cancel()
    holder.release()
    await asyncio.to_thread(waiting.join, _HOLD * 2)
    await lock.close()
