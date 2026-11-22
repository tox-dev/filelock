from __future__ import annotations

import asyncio
import time
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING, Final, Literal

import pytest

from filelock import (
    AsyncFileLock,
    AsyncSoftFileLock,
    BaseAsyncFileLock,
    Timeout,
)

if TYPE_CHECKING:
    from pathlib import Path

    from pytest_mock import MockerFixture


@pytest.mark.parametrize("lock_type", [AsyncFileLock, AsyncSoftFileLock])
@pytest.mark.parametrize("timeout", [pytest.param(-1, id="unlimited"), pytest.param(float("inf"), id="infinite")])
@pytest.mark.asyncio
async def test_same_task_different_instances_raises(
    tmp_path: Path, lock_type: type[BaseAsyncFileLock], timeout: float
) -> None:
    lock_path = tmp_path / "test.lock"
    async with lock_type(lock_path):
        lock2 = lock_type(lock_path, timeout=timeout)
        with pytest.raises(RuntimeError, match="Deadlock"):
            await lock2.acquire()


@pytest.mark.parametrize("lock_type", [AsyncFileLock, AsyncSoftFileLock])
@pytest.mark.asyncio
async def test_finite_timeout_gives_timeout_not_deadlock(tmp_path: Path, lock_type: type[BaseAsyncFileLock]) -> None:
    lock_path = tmp_path / "test.lock"
    async with lock_type(lock_path):
        lock2 = lock_type(lock_path, timeout=0.1)
        with pytest.raises(Timeout):
            await lock2.acquire()


@pytest.mark.parametrize("lock_type", [AsyncFileLock, AsyncSoftFileLock])
@pytest.mark.parametrize(
    ("lock_timeout", "call_timeout"),
    [
        pytest.param(float("nan"), None, id="default"),
        pytest.param(-1, float("nan"), id="argument"),
    ],
)
@pytest.mark.asyncio
async def test_blocking_acquire_rejects_nan_timeout(
    lock_type: type[BaseAsyncFileLock], lock_timeout: float, call_timeout: float | None, tmp_path: Path
) -> None:
    lock = lock_type(tmp_path / "a", timeout=lock_timeout)
    with pytest.raises(ValueError, match="not nan"):
        await lock.acquire(timeout=call_timeout)
    assert not lock.is_locked


@pytest.mark.parametrize("lock_type", [AsyncFileLock, AsyncSoftFileLock])
@pytest.mark.asyncio
async def test_non_blocking_acquire_ignores_nan_timeout(lock_type: type[BaseAsyncFileLock], tmp_path: Path) -> None:
    lock = lock_type(tmp_path / "a")
    async with await lock.acquire(timeout=float("nan"), blocking=False):
        assert lock.is_locked


@pytest.mark.parametrize("lock_type", [AsyncFileLock, AsyncSoftFileLock])
@pytest.mark.asyncio
async def test_poll_wait_stays_within_timeout(tmp_path: Path, lock_type: type[BaseAsyncFileLock]) -> None:
    lock_path = tmp_path / "test.lock"
    async with lock_type(lock_path):
        start = time.perf_counter()
        with pytest.raises(Timeout):
            await lock_type(lock_path).acquire(timeout=0.05, poll_interval=10)
        assert time.perf_counter() - start < 5


@pytest.mark.parametrize("lock_type", [AsyncFileLock, AsyncSoftFileLock])
@pytest.mark.asyncio
async def test_non_blocking_gives_timeout_not_deadlock(tmp_path: Path, lock_type: type[BaseAsyncFileLock]) -> None:
    lock_path = tmp_path / "test.lock"
    async with lock_type(lock_path):
        lock2 = lock_type(lock_path, blocking=False)
        with pytest.raises(Timeout):
            await lock2.acquire()


@pytest.mark.parametrize(
    "mode",
    [pytest.param("finite", id="finite"), pytest.param("nonblocking", id="nonblocking")],
)
@pytest.mark.asyncio
async def test_failed_acquire_keeps_holder_registered(tmp_path: Path, mode: Literal["finite", "nonblocking"]) -> None:
    lock_path = tmp_path / "test.lock"
    async with AsyncFileLock(lock_path, run_in_executor=False):
        with pytest.raises(Timeout):
            await _acquire_for_mode(AsyncFileLock(lock_path, run_in_executor=False), mode)
        with pytest.raises(RuntimeError, match="Deadlock"):
            await AsyncFileLock(lock_path, run_in_executor=False).acquire(cancel_check=lambda: True)


async def _acquire_for_mode(lock: BaseAsyncFileLock, mode: Literal["finite", "nonblocking"]) -> None:
    if mode == "finite":
        await lock.acquire(timeout=0)
    else:
        await lock.acquire(blocking=False)


@pytest.mark.parametrize("lock_type", [AsyncFileLock, AsyncSoftFileLock])
@pytest.mark.asyncio
async def test_different_paths_no_conflict(tmp_path: Path, lock_type: type[BaseAsyncFileLock]) -> None:
    lock1 = lock_type(tmp_path / "a.lock")
    lock2 = lock_type(tmp_path / "b.lock")
    async with lock1, lock2:
        assert lock1.is_locked
        assert lock2.is_locked


@pytest.mark.parametrize("lock_type", [AsyncFileLock, AsyncSoftFileLock])
@pytest.mark.asyncio
async def test_same_instance_reentrant_works(tmp_path: Path, lock_type: type[BaseAsyncFileLock]) -> None:
    lock = lock_type(tmp_path / "test.lock")
    async with lock:
        async with lock:
            assert lock.is_locked
        assert lock.is_locked
    assert not lock.is_locked


@pytest.mark.parametrize("lock_type", [AsyncFileLock, AsyncSoftFileLock])
@pytest.mark.asyncio
async def test_same_instance_reentrant_acquire_skips_the_executor(
    tmp_path: Path, lock_type: type[BaseAsyncFileLock], mocker: MockerFixture
) -> None:
    executor: Final[ThreadPoolExecutor] = ThreadPoolExecutor(max_workers=1)
    lock: Final[BaseAsyncFileLock] = lock_type(tmp_path / "test.lock", executor=executor)
    async with lock:
        submit = mocker.spy(executor, "submit")
        async with lock:
            assert (lock.lock_counter, submit.call_count) == (2, 0)
    executor.shutdown()


@pytest.mark.parametrize("lock_type", [AsyncFileLock, AsyncSoftFileLock])
@pytest.mark.asyncio
async def test_singleton_avoids_deadlock(tmp_path: Path, lock_type: type[BaseAsyncFileLock]) -> None:
    lock_path = tmp_path / "test.lock"
    lock1 = lock_type(lock_path, is_singleton=True)
    async with lock1:
        lock2 = lock_type(lock_path, is_singleton=True)
        assert lock1 is lock2
        async with lock2:
            assert lock2.is_locked


@pytest.mark.parametrize("lock_type", [AsyncFileLock, AsyncSoftFileLock])
@pytest.mark.asyncio
async def test_different_tasks_no_false_positive(tmp_path: Path, lock_type: type[BaseAsyncFileLock]) -> None:
    # The registry is per-thread, so a second acquire from a different task must not look like a reentrant deadlock.
    lock_path = tmp_path / "test.lock"
    lock1 = lock_type(lock_path, timeout=0)
    await lock1.acquire()

    error: BaseException | None = None

    async def acquire_other() -> None:
        nonlocal error
        lock2 = lock_type(lock_path, timeout=0)
        try:
            await lock2.acquire()
        except BaseException as exc:
            error = exc

    await asyncio.create_task(acquire_other())
    await lock1.release()

    assert not isinstance(error, RuntimeError), "Should not raise RuntimeError in a different task"


@pytest.mark.parametrize("lock_type", [AsyncFileLock, AsyncSoftFileLock])
@pytest.mark.asyncio
async def test_cross_task_blocking_acquire_queues(tmp_path: Path, lock_type: type[BaseAsyncFileLock]) -> None:
    # A blocking acquire from a different task must queue behind the holder, not raise. Polling yields the event
    # loop on every iteration, so the holder keeps running and releases; only a same-task reentry can deadlock.
    # The waiter starts on an event the holder sets once it holds: yielding with sleep(0) instead would let the
    # waiter run its check before the holder registers, so an empty registry would pass even with the bug.
    lock_path = tmp_path / "test.lock"
    events: list[str] = []
    holding = asyncio.Event()

    async def holder() -> None:
        async with lock_type(lock_path):
            events.append("holder:acquired")
            holding.set()
            await asyncio.sleep(  # simplint: ignore[sleep-in-test]  # needs a queued-acquire signal
                0.05
            )  # the waiter polls while the lock is held
            events.append("holder:released")

    async def waiter() -> None:
        await holding.wait()
        async with lock_type(lock_path):
            events.append("waiter:acquired")

    await asyncio.gather(holder(), waiter())  # propagates a false-positive Deadlock

    assert events == ["holder:acquired", "holder:released", "waiter:acquired"]


@pytest.mark.parametrize("lock_type", [AsyncFileLock, AsyncSoftFileLock])
@pytest.mark.asyncio
async def test_release_from_different_task_clears_registry(tmp_path: Path, lock_type: type[BaseAsyncFileLock]) -> None:
    # The holder scope is pinned at commit time, so releasing from a different task still drops the registry entry.
    lock_path = tmp_path / "test.lock"
    lock = lock_type(lock_path)
    await lock.acquire()

    async def release() -> None:
        await lock.release()

    await asyncio.create_task(release())

    async with lock_type(lock_path):  # would raise Deadlock if the scoped entry had leaked
        pass


@pytest.mark.parametrize("lock_type", [AsyncFileLock, AsyncSoftFileLock])
@pytest.mark.asyncio
async def test_deadlock_registry_cleanup_on_release(tmp_path: Path, lock_type: type[BaseAsyncFileLock]) -> None:
    lock_path = tmp_path / "test.lock"
    lock1 = lock_type(lock_path)
    await lock1.acquire()
    await lock1.release()

    lock2 = lock_type(lock_path)
    async with lock2:
        assert lock2.is_locked


@pytest.mark.parametrize("lock_type", [AsyncFileLock, AsyncSoftFileLock])
@pytest.mark.asyncio
async def test_force_release_clears_registry(tmp_path: Path, lock_type: type[BaseAsyncFileLock]) -> None:
    lock_path = tmp_path / "test.lock"
    lock1 = lock_type(lock_path)
    async with lock1:
        await lock1.acquire()
        assert lock1.lock_counter == 2
    await lock1.release(force=True)

    lock2 = lock_type(lock_path)
    async with lock2:
        assert lock2.is_locked
