from __future__ import annotations

import asyncio
import gc
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Final

import pytest

from tests.capability_marks import (
    NEEDS_GC_OBJECTS,
    XFAIL_WITHOUT_COROUTINE_CANCELLATION,
)

pytest.importorskip("sqlite3")


from filelock import AsyncReadWriteLock, Timeout
from tests.sqlite.async_helpers import TASK_WAIT, can_write, write_once

pytestmark: Final[pytest.MarkDecorator] = pytest.mark.usefixtures("clear_sqlite_singletons")


@pytest.mark.asyncio
async def test_tasks_sharing_a_lock_take_the_write_lock_one_at_a_time(lock_file: str) -> None:
    assert await writers_peak_inside(AsyncReadWriteLock(lock_file, is_singleton=False)) == 1


async def writers_peak_inside(lock: AsyncReadWriteLock) -> int:
    inside = peak = 0

    async def write() -> None:
        nonlocal inside, peak
        async with lock.write_lock(timeout=TASK_WAIT):
            inside += 1
            peak = max(peak, inside)
            await asyncio.sleep(0)
            inside -= 1

    await asyncio.gather(*(write() for _ in range(20)))
    await lock.close()
    return peak


@pytest.mark.asyncio
async def test_tasks_sharing_a_lock_take_the_write_lock_one_at_a_time_with_four_workers(lock_file: str) -> None:
    executor = ThreadPoolExecutor(max_workers=4)
    assert await writers_peak_inside(AsyncReadWriteLock(lock_file, is_singleton=False, executor=executor)) == 1
    executor.shutdown()


@pytest.mark.asyncio
async def test_tasks_sharing_a_lock_hold_the_read_lock_together(lock_file: str) -> None:
    lock = AsyncReadWriteLock(lock_file, is_singleton=False)
    readers = 0
    both_inside = asyncio.Event()

    async def read() -> None:
        nonlocal readers
        async with lock.read_lock(timeout=TASK_WAIT):
            readers += 1
            if readers == 2:
                both_inside.set()
            await asyncio.wait_for(both_inside.wait(), TASK_WAIT)

    await asyncio.gather(read(), read())
    await lock.close()

    assert readers == 2


@pytest.mark.asyncio
async def test_another_task_waits_for_the_last_release_of_a_nested_write(lock_file: str) -> None:
    lock = AsyncReadWriteLock(lock_file, is_singleton=False)
    await lock.acquire_write()
    await lock.acquire_write()
    await lock.release()
    entered_while_nested = await asyncio.create_task(can_write(lock))
    await lock.release()
    entered_after_release = await asyncio.create_task(can_write(lock))
    await lock.close()

    assert (entered_while_nested, entered_after_release) == (False, True)


@pytest.mark.asyncio
async def test_force_release_drops_the_callers_whole_nest(lock_file: str) -> None:
    lock = AsyncReadWriteLock(lock_file, is_singleton=False)
    await lock.acquire_write()
    await lock.acquire_write()
    await lock.release(force=True)
    entered = await asyncio.create_task(can_write(lock))
    await lock.close()

    assert entered


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
    writer = asyncio.create_task(write_once(lock))
    await asyncio.sleep(0)  # the writer registers as waiting before its first suspension
    with pytest.raises(Timeout):
        await asyncio.create_task(lock.acquire_read(blocking=False))
    await lock.release()
    await asyncio.wait_for(writer, TASK_WAIT)
    await lock.close()


@pytest.mark.asyncio
async def test_many_waiting_writers_drain_after_reader_releases(lock_file: str) -> None:
    lock = AsyncReadWriteLock(lock_file, is_singleton=False)
    await lock.acquire_read()
    started = 0
    all_started = asyncio.Event()
    completed: list[int] = []
    writer_count = 1200

    async def write(position: int) -> None:
        nonlocal started
        started += 1
        if started == writer_count:
            all_started.set()
        async with lock.write_lock(timeout=TASK_WAIT):
            completed.append(position)

    tasks = [asyncio.create_task(write(index)) for index in range(writer_count)]
    await asyncio.wait_for(all_started.wait(), TASK_WAIT)
    await asyncio.sleep(0)
    assert not any(task.done() for task in tasks)
    await lock.release()
    await asyncio.wait_for(asyncio.gather(*tasks), 5)
    await lock.close()

    assert completed == list(range(writer_count))


@pytest.mark.asyncio
async def test_queued_writer_precedes_a_reacquiring_writer(lock_file: str) -> None:
    lock = AsyncReadWriteLock(lock_file, is_singleton=False)
    order: list[str] = []

    async def queued() -> None:
        async with lock.write_lock(timeout=TASK_WAIT):
            order.append("queued")

    async with lock.write_lock():
        waiting = asyncio.create_task(queued())
        await asyncio.sleep(0)
    async with lock.write_lock(timeout=TASK_WAIT):
        order.append("returning")
    await waiting
    await lock.close()

    assert order == ["queued", "returning"]


@pytest.mark.asyncio
@XFAIL_WITHOUT_COROUTINE_CANCELLATION
async def test_a_canceled_waiting_writer_lets_readers_in(lock_file: str) -> None:
    lock = AsyncReadWriteLock(lock_file, is_singleton=False)
    await lock.acquire_read()
    writer = asyncio.create_task(write_once(lock))
    await asyncio.sleep(0)
    writer.cancel()
    with pytest.raises(asyncio.CancelledError):
        await writer
    await asyncio.create_task(_read_without_waiting(lock))
    await lock.release()
    await lock.close()


async def _read_without_waiting(lock: AsyncReadWriteLock) -> None:
    async with lock.read_lock(blocking=False):
        pass


@pytest.mark.asyncio
async def test_a_writer_times_out_waiting_for_another_task(lock_file: str) -> None:
    lock = AsyncReadWriteLock(lock_file, is_singleton=False)
    await lock.acquire_write()
    with pytest.raises(Timeout):
        await asyncio.create_task(lock.acquire_write(timeout=0.05))
    await lock.release()
    await lock.close()


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
    error = await asyncio.wait_for(outcome, TASK_WAIT)
    await lock.close()

    assert "inside an asyncio task" in str(error)


def test_tasks_on_different_event_loops_exclude_each_other(lock_file: str) -> None:
    guard = threading.Lock()
    inside = peak = 0

    async def write_repeatedly() -> None:
        nonlocal inside, peak
        lock = AsyncReadWriteLock(lock_file)
        for _ in range(20):
            async with lock.write_lock(timeout=TASK_WAIT):
                with guard:
                    inside += 1
                    peak = max(peak, inside)
                await asyncio.sleep(0)
                with guard:
                    inside -= 1

    threads = [threading.Thread(target=asyncio.run, args=(write_repeatedly(),), daemon=True) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(TASK_WAIT)

    assert ([thread.is_alive() for thread in threads], peak) == ([False, False], 1)


@NEEDS_GC_OBJECTS
@pytest.mark.asyncio
async def test_async_reads_giving_up_behind_a_writer_leave_nothing_behind(lock_file: str) -> None:
    lock: Final[AsyncReadWriteLock] = AsyncReadWriteLock(lock_file, is_singleton=False)
    await lock.acquire_write()
    await asyncio.create_task(_give_up_reading(lock))
    before: Final[int] = _live_asyncio_futures()
    give_ups: Final[int] = 200

    for _ in range(give_ups):
        await asyncio.create_task(_give_up_reading(lock))

    # A leaked future per timeout dwarfs fluctuations in the test runner's own futures.
    assert _live_asyncio_futures() - before < give_ups // 4
    await lock.release()
    await lock.close()


async def _give_up_reading(lock: AsyncReadWriteLock) -> None:
    with pytest.raises(Timeout):
        await lock.acquire_read(timeout=0.001)


def _live_asyncio_futures() -> int:
    gc.collect()
    return sum(issubclass(type(candidate), asyncio.Future) for candidate in gc.get_objects())
