from __future__ import annotations

import asyncio
import gc
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING, Final, Literal

import pytest

from tests.capability_marks import NEEDS_COLLECTED_FINALIZATION, XFAIL_WITHOUT_COROUTINE_CANCELLATION

pytest.importorskip("sqlite3")

import sqlite3

from filelock import AsyncReadWriteLock, ReadWriteLock, Timeout

if TYPE_CHECKING:
    from collections.abc import Generator
    from pathlib import Path

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


@pytest.mark.parametrize("mode", [pytest.param("read", id="read"), pytest.param("write", id="write")])
@pytest.mark.asyncio
async def test_acquire_release(lock_file: str, mode: Literal["read", "write"]) -> None:
    lock = AsyncReadWriteLock(lock_file, is_singleton=False)
    proxy = await (lock.acquire_read() if mode == "read" else lock.acquire_write())
    async with proxy as held:
        assert held is lock
    with pytest.raises(RuntimeError, match="not held"):
        await lock.release()
    await lock.close()


@pytest.mark.parametrize("mode", [pytest.param("read", id="read"), pytest.param("write", id="write")])
@pytest.mark.asyncio
async def test_lock_context_manager(lock_file: str, mode: Literal["read", "write"]) -> None:
    lock = AsyncReadWriteLock(lock_file, is_singleton=False)
    ctx = lock.read_lock() if mode == "read" else lock.write_lock()
    async with ctx:
        assert_mode_held(lock_file, mode)
    with pytest.raises(RuntimeError, match="not held"):
        await lock.release()
    await lock.close()


@pytest.mark.parametrize("mode", [pytest.param("read", id="read"), pytest.param("write", id="write")])
@pytest.mark.asyncio
async def test_reentrant(lock_file: str, mode: Literal["read", "write"]) -> None:
    lock = AsyncReadWriteLock(lock_file, is_singleton=False)
    acquire = lock.acquire_read if mode == "read" else lock.acquire_write
    await acquire()
    await acquire()
    await lock.release()
    assert_mode_held(lock_file, mode)
    await lock.release()
    with pytest.raises(RuntimeError, match="not held"):
        await lock.release()
    await lock.close()


@pytest.mark.parametrize(
    ("held", "requested", "match"),
    [
        pytest.param("read", "write", r"already holding a read lock.*upgrade not allowed", id="upgrade"),
        pytest.param("write", "read", r"already holding a write lock.*downgrade not allowed", id="downgrade"),
    ],
)
@pytest.mark.asyncio
async def test_mode_change_prohibited(
    lock_file: str,
    held: Literal["read", "write"],
    requested: Literal["read", "write"],
    match: str,
) -> None:
    lock = AsyncReadWriteLock(lock_file, is_singleton=False)
    await (lock.acquire_read() if held == "read" else lock.acquire_write())
    with pytest.raises(RuntimeError, match=match):
        await (lock.acquire_read() if requested == "read" else lock.acquire_write())
    await lock.release()
    await lock.close()


@pytest.mark.parametrize("mode", [pytest.param("read", id="read"), pytest.param("write", id="write")])
@pytest.mark.asyncio
async def test_non_blocking_conflict(lock_file: str, mode: Literal["read", "write"]) -> None:
    holder = ReadWriteLock(lock_file, is_singleton=False)
    (holder.acquire_write if mode == "read" else holder.acquire_read)()
    try:
        lock = AsyncReadWriteLock(lock_file, is_singleton=False)
        with pytest.raises(Timeout):
            await (lock.acquire_read if mode == "read" else lock.acquire_write)(blocking=False)
        await lock.close()
    finally:
        holder.release()


@pytest.mark.asyncio
async def test_timeout_expires(lock_file: str) -> None:
    holder = ReadWriteLock(lock_file, is_singleton=False)
    holder.acquire_write()
    try:
        lock = AsyncReadWriteLock(lock_file, is_singleton=False)
        with pytest.raises(Timeout):
            await lock.acquire_read(timeout=0.2)
        await lock.close()
    finally:
        holder.release()


@pytest.mark.asyncio
async def test_release_unheld_raises(lock_file: str) -> None:
    lock = AsyncReadWriteLock(lock_file, is_singleton=False)
    with pytest.raises(RuntimeError, match="not held"):
        await lock.release()
    await lock.close()


@pytest.mark.asyncio
async def test_release_force(lock_file: str) -> None:
    lock = AsyncReadWriteLock(lock_file, is_singleton=False)
    await lock.acquire_write()
    await lock.acquire_write()
    await lock.release(force=True)
    with pytest.raises(RuntimeError, match="not held"):
        await lock.release()
    await lock.close()


@pytest.mark.asyncio
async def test_release_force_unheld_is_noop(lock_file: str) -> None:
    lock = AsyncReadWriteLock(lock_file, is_singleton=False)
    await lock.release(force=True)
    await lock.close()


@pytest.mark.parametrize(
    ("held", "supplied_executor"),
    [
        pytest.param(False, False, id="idle-owned-executor"),
        pytest.param(True, False, id="held-owned-executor"),
        pytest.param(False, True, id="idle-supplied-executor"),
        pytest.param(True, True, id="held-supplied-executor"),
    ],
)
@pytest.mark.asyncio
async def test_close_rejects_later_acquisition(lock_file: str, held: bool, supplied_executor: bool) -> None:
    executor = ThreadPoolExecutor(max_workers=1) if supplied_executor else None
    lock = AsyncReadWriteLock(lock_file, is_singleton=False, executor=executor)
    if held:
        await lock.acquire_write()
    await lock.close()
    with pytest.raises(sqlite3.ProgrammingError, match="Cannot operate on a closed database"):
        await lock.acquire_read()
    if executor is not None:
        executor.shutdown(wait=False)


def test_properties(lock_file: str) -> None:
    executor = ThreadPoolExecutor(max_workers=1)
    lock = AsyncReadWriteLock(lock_file, timeout=5.0, blocking=False, is_singleton=False, executor=executor)
    assert lock.lock_file == lock_file
    assert lock.timeout == pytest.approx(5.0)
    assert lock.blocking is False
    assert lock.loop is None
    assert lock.executor is executor
    executor.shutdown(wait=False)


@pytest.mark.asyncio
async def test_custom_executor(lock_file: str) -> None:
    executor = ThreadPoolExecutor(max_workers=1)
    lock = AsyncReadWriteLock(lock_file, is_singleton=False, executor=executor)
    async with lock.read_lock():
        assert_mode_held(lock_file, "read")
    with pytest.raises(RuntimeError, match="not held"):
        await lock.release()
    executor.shutdown(wait=False)


@pytest.mark.asyncio
async def test_close_shuts_down_owned_executor(lock_file: str) -> None:
    lock = AsyncReadWriteLock(lock_file, is_singleton=False)
    executor = lock.executor
    await lock.close()
    await lock.close()
    with pytest.raises(RuntimeError):
        executor.submit(int)


@pytest.mark.asyncio
async def test_close_keeps_provided_executor_open(lock_file: str) -> None:
    executor = ThreadPoolExecutor(max_workers=1)
    lock = AsyncReadWriteLock(lock_file, is_singleton=False, executor=executor)
    await lock.close()
    assert executor.submit(int).result(timeout=5) == 0
    executor.shutdown(wait=False)


@NEEDS_COLLECTED_FINALIZATION
def test_del_shuts_down_owned_executor(lock_file: str) -> None:
    lock = AsyncReadWriteLock(lock_file, is_singleton=False)
    executor = lock.executor
    del lock
    gc.collect()
    with pytest.raises(RuntimeError):
        executor.submit(int)


def test_del_keeps_provided_executor_open(lock_file: str) -> None:
    executor = ThreadPoolExecutor(max_workers=1)
    lock = AsyncReadWriteLock(lock_file, is_singleton=False, executor=executor)
    del lock
    gc.collect()
    assert executor.submit(int).result(timeout=5) == 0
    executor.shutdown(wait=False)


@pytest.mark.asyncio
async def test_acquire_return_proxy_context_manager(lock_file: str) -> None:
    lock = AsyncReadWriteLock(lock_file, is_singleton=False)
    async with await lock.acquire_read() as ctx:
        assert ctx is lock
        assert_mode_held(lock_file, "read")
    with pytest.raises(RuntimeError, match="not held"):
        await lock.release()
    await lock.close()


@pytest.mark.parametrize("mode", [pytest.param("read", id="read"), pytest.param("write", id="write")])
@pytest.mark.asyncio
async def test_nested_context_managers(lock_file: str, mode: Literal["read", "write"]) -> None:
    lock = AsyncReadWriteLock(lock_file, is_singleton=False)
    make_ctx = lock.read_lock if mode == "read" else lock.write_lock
    async with make_ctx():
        assert_mode_held(lock_file, mode)
        async with make_ctx():
            assert_mode_held(lock_file, mode)
        assert_mode_held(lock_file, mode)
    with pytest.raises(RuntimeError, match="not held"):
        await lock.release()
    await lock.close()


@pytest.mark.asyncio
async def test_context_manager_uses_instance_defaults(lock_file: str) -> None:
    lock = AsyncReadWriteLock(lock_file, timeout=3.0, blocking=True, is_singleton=False)
    async with lock.read_lock():
        assert_mode_held(lock_file, "read")
    async with lock.write_lock():
        assert_mode_held(lock_file, "write")
    await lock.close()


@pytest.mark.asyncio
async def test_context_manager_overrides_defaults(lock_file: str) -> None:
    lock = AsyncReadWriteLock(lock_file, timeout=10.0, blocking=False, is_singleton=False)
    async with lock.read_lock(timeout=5.0, blocking=True):
        assert_mode_held(lock_file, "read")
    async with lock.write_lock(timeout=5.0, blocking=True):
        assert_mode_held(lock_file, "write")
    await lock.close()


@pytest.mark.asyncio
async def test_sequential_mode_switch(lock_file: str) -> None:
    lock = AsyncReadWriteLock(lock_file, is_singleton=False)
    async with lock.read_lock():
        pass
    async with lock.write_lock():
        pass
    async with lock.read_lock():
        pass
    await lock.close()


@pytest.mark.parametrize("workers", [pytest.param(None, id="owned-executor"), pytest.param(4, id="four-workers")])
@pytest.mark.asyncio
async def test_tasks_sharing_a_lock_take_the_write_lock_one_at_a_time(lock_file: str, workers: int | None) -> None:
    executor = None if workers is None else ThreadPoolExecutor(max_workers=workers)
    lock = AsyncReadWriteLock(lock_file, is_singleton=False, executor=executor)
    inside = peak = 0

    async def write() -> None:
        nonlocal inside, peak
        async with lock.write_lock(timeout=_TASK_WAIT):
            inside += 1
            peak = max(peak, inside)
            await asyncio.sleep(0.001)
            inside -= 1

    await asyncio.gather(*(write() for _ in range(20)))
    await lock.close()
    if executor is not None:
        executor.shutdown()

    assert peak == 1


@pytest.mark.asyncio
async def test_tasks_sharing_a_lock_hold_the_read_lock_together(lock_file: str) -> None:
    lock = AsyncReadWriteLock(lock_file, is_singleton=False)
    readers = 0
    both_inside = asyncio.Event()

    async def read() -> None:
        nonlocal readers
        async with lock.read_lock(timeout=_TASK_WAIT):
            readers += 1
            if readers == 2:
                both_inside.set()
            await asyncio.wait_for(both_inside.wait(), _TASK_WAIT)

    await asyncio.gather(read(), read())
    await lock.close()

    assert readers == 2


@pytest.mark.asyncio
async def test_another_task_waits_for_the_last_release_of_a_nested_write(lock_file: str) -> None:
    lock = AsyncReadWriteLock(lock_file, is_singleton=False)
    await lock.acquire_write()
    await lock.acquire_write()
    await lock.release()
    entered_while_nested = await asyncio.create_task(_try_write(lock))
    await lock.release()
    entered_after_release = await asyncio.create_task(_try_write(lock))
    await lock.close()

    assert (entered_while_nested, entered_after_release) == (False, True)


@pytest.mark.asyncio
async def test_force_release_drops_the_callers_whole_nest(lock_file: str) -> None:
    lock = AsyncReadWriteLock(lock_file, is_singleton=False)
    await lock.acquire_write()
    await lock.acquire_write()
    await lock.release(force=True)
    entered = await asyncio.create_task(_try_write(lock))
    await lock.close()

    assert entered


async def _try_write(lock: AsyncReadWriteLock) -> bool:
    try:
        await lock.acquire_write(blocking=False)
    except Timeout:
        return False
    await lock.release()
    return True


@pytest.mark.asyncio
async def test_release_from_a_task_that_does_not_hold_raises(lock_file: str) -> None:
    lock = AsyncReadWriteLock(lock_file, is_singleton=False)
    await lock.acquire_write()
    with pytest.raises(RuntimeError, match="not held by this task"):
        await asyncio.create_task(lock.release())
    await lock.release()
    await lock.close()


@pytest.mark.asyncio
async def test_a_waiting_writer_goes_ahead_of_new_readers(lock_file: str) -> None:
    lock = AsyncReadWriteLock(lock_file, is_singleton=False)
    await lock.acquire_read()
    writer = asyncio.create_task(_write_once(lock))
    await asyncio.sleep(0)  # the writer registers as waiting before its first suspension
    with pytest.raises(Timeout):
        await asyncio.create_task(lock.acquire_read(blocking=False))
    await lock.release()
    await asyncio.wait_for(writer, _TASK_WAIT)
    await lock.close()


@pytest.mark.asyncio
@XFAIL_WITHOUT_COROUTINE_CANCELLATION
async def test_a_canceled_waiting_writer_lets_readers_in(lock_file: str) -> None:
    lock = AsyncReadWriteLock(lock_file, is_singleton=False)
    await lock.acquire_read()
    writer = asyncio.create_task(_write_once(lock))
    await asyncio.sleep(0)
    writer.cancel()
    with pytest.raises(asyncio.CancelledError):
        await writer
    await asyncio.create_task(_read_without_waiting(lock))
    await lock.release()
    await lock.close()


@pytest.mark.asyncio
async def test_a_writer_times_out_waiting_for_another_task(lock_file: str) -> None:
    lock = AsyncReadWriteLock(lock_file, is_singleton=False)
    await lock.acquire_write()
    with pytest.raises(Timeout):
        await asyncio.create_task(lock.acquire_write(timeout=0.05))
    await lock.release()
    await lock.close()


async def _write_once(lock: AsyncReadWriteLock) -> None:
    async with lock.write_lock(timeout=_TASK_WAIT):
        pass


async def _read_without_waiting(lock: AsyncReadWriteLock) -> None:
    async with lock.read_lock(blocking=False):
        pass


@pytest.mark.asyncio
async def test_acquire_outside_a_task_raises(lock_file: str) -> None:
    lock = AsyncReadWriteLock(lock_file, is_singleton=False)
    loop = asyncio.get_running_loop()
    outcome: asyncio.Future[BaseException] = loop.create_future()

    def drive_outside_any_task() -> None:
        try:
            lock.acquire_write().send(None)
        except RuntimeError as error:
            outcome.set_result(error)

    loop.call_soon(drive_outside_any_task)
    error = await asyncio.wait_for(outcome, _TASK_WAIT)
    await lock.close()

    assert "inside an asyncio task" in str(error)


def test_tasks_on_different_event_loops_exclude_each_other(lock_file: str) -> None:
    guard = threading.Lock()
    inside = peak = 0

    async def write_repeatedly() -> None:
        nonlocal inside, peak
        lock = AsyncReadWriteLock(lock_file)
        for _ in range(20):
            async with lock.write_lock(timeout=_TASK_WAIT):
                with guard:
                    inside += 1
                    peak = max(peak, inside)
                await asyncio.sleep(0.001)
                with guard:
                    inside -= 1

    threads = [threading.Thread(target=asyncio.run, args=(write_repeatedly(),), daemon=True) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(_TASK_WAIT)

    assert ([thread.is_alive() for thread in threads], peak) == ([False, False], 1)


def assert_mode_held(lock_file: str, mode: Literal["read", "write"]) -> None:
    contender = ReadWriteLock(lock_file, is_singleton=False)
    acquire = contender.acquire_write if mode == "read" else contender.acquire_read
    with pytest.raises(Timeout):
        acquire(blocking=False)
    contender.close()
