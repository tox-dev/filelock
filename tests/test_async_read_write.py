from __future__ import annotations

import asyncio
import gc
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING, Final, Literal

import pytest
import pytest_asyncio

from tests.capability_marks import NEEDS_COLLECTED_FINALIZATION, XFAIL_WITHOUT_COROUTINE_CANCELLATION
from tests.read_write_helpers import ACQUIRE_SETTINGS, INVALID_TIMEOUTS, OPPOSITE_MODE, Interrupt, interrupt_once

pytest.importorskip("sqlite3")

import sqlite3

from filelock import AsyncReadWriteLock, ReadWriteLock, Timeout

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable, Generator
    from pathlib import Path

    from pytest_mock import MockerFixture

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


@pytest.mark.asyncio
async def test_async_release_leaves_a_sync_reader_of_the_same_singleton_holding(lock_file: str) -> None:
    async_lock: Final = AsyncReadWriteLock(lock_file)
    with ReadWriteLock(lock_file).read_lock():
        async with async_lock.read_lock():
            pass
        with pytest.raises(Timeout):
            ReadWriteLock(lock_file, is_singleton=False).acquire_write(blocking=False)
    await async_lock.close()


@pytest.mark.parametrize("mode", [pytest.param("read", id="read"), pytest.param("write", id="write")])
@pytest.mark.asyncio
async def test_acquire_release(lock_file: str, mode: Literal["read", "write"]) -> None:
    lock = AsyncReadWriteLock(lock_file, is_singleton=False)
    proxy = await getattr(lock, f"acquire_{mode}")()
    async with proxy as held:
        assert held is lock
    with pytest.raises(RuntimeError, match="not held"):
        await lock.release()
    await lock.close()


@pytest.mark.parametrize("mode", [pytest.param("read", id="read"), pytest.param("write", id="write")])
@pytest.mark.asyncio
async def test_lock_context_manager(lock_file: str, mode: Literal["read", "write"]) -> None:
    lock = AsyncReadWriteLock(lock_file, is_singleton=False)
    async with getattr(lock, f"{mode}_lock")():
        assert_mode_held(lock_file, mode)
    with pytest.raises(RuntimeError, match="not held"):
        await lock.release()
    await lock.close()


def assert_mode_held(lock_file: str, mode: Literal["read", "write"]) -> None:
    contender = ReadWriteLock(lock_file, is_singleton=False)
    acquire = getattr(contender, f"acquire_{OPPOSITE_MODE[mode]}")
    with pytest.raises(Timeout):
        acquire(blocking=False)
    contender.close()


@pytest.mark.parametrize("mode", [pytest.param("read", id="read"), pytest.param("write", id="write")])
@pytest.mark.asyncio
async def test_reentrant(lock_file: str, mode: Literal["read", "write"]) -> None:
    lock = AsyncReadWriteLock(lock_file, is_singleton=False)
    acquire = getattr(lock, f"acquire_{mode}")
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
    await getattr(lock, f"acquire_{held}")()
    with pytest.raises(RuntimeError, match=match):
        await getattr(lock, f"acquire_{requested}")()
    await lock.release()
    await lock.close()


@pytest.mark.parametrize(
    ("mode", "held"),
    [
        pytest.param("read", "write", id="read-blocked-by-write"),
        pytest.param("write", "read", id="write-blocked-by-read"),
    ],
)
@pytest.mark.asyncio
async def test_non_blocking_conflict(
    lock_file: str, mode: Literal["read", "write"], held: Literal["read", "write"]
) -> None:
    holder = ReadWriteLock(lock_file, is_singleton=False)
    getattr(holder, f"acquire_{held}")()
    try:
        lock = AsyncReadWriteLock(lock_file, is_singleton=False)
        with pytest.raises(Timeout):
            await getattr(lock, f"acquire_{mode}")(blocking=False)
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


@pytest.mark.asyncio
async def test_close_rejects_later_acquisition_idle_owned_executor(lock_file: str) -> None:
    lock = AsyncReadWriteLock(lock_file, is_singleton=False)
    await lock.close()
    with pytest.raises(sqlite3.ProgrammingError, match="Cannot operate on a closed database"):
        await lock.acquire_read()


@pytest.mark.asyncio
async def test_close_rejects_later_acquisition_held_owned_executor(lock_file: str) -> None:
    lock = AsyncReadWriteLock(lock_file, is_singleton=False)
    await lock.acquire_write()
    await lock.close()
    with pytest.raises(sqlite3.ProgrammingError, match="Cannot operate on a closed database"):
        await lock.acquire_read()


@pytest.mark.asyncio
async def test_close_rejects_later_acquisition_idle_supplied_executor(lock_file: str) -> None:
    executor = ThreadPoolExecutor(max_workers=1)
    lock = AsyncReadWriteLock(lock_file, is_singleton=False, executor=executor)
    await lock.close()
    with pytest.raises(sqlite3.ProgrammingError, match="Cannot operate on a closed database"):
        await lock.acquire_read()
    executor.shutdown(wait=False)


@pytest.mark.asyncio
async def test_close_rejects_later_acquisition_held_supplied_executor(lock_file: str) -> None:
    executor = ThreadPoolExecutor(max_workers=1)
    lock = AsyncReadWriteLock(lock_file, is_singleton=False, executor=executor)
    await lock.acquire_write()
    await lock.close()
    with pytest.raises(sqlite3.ProgrammingError, match="Cannot operate on a closed database"):
        await lock.acquire_read()
    executor.shutdown(wait=False)


def test_properties(lock_file: str) -> None:
    executor = ThreadPoolExecutor(max_workers=1)
    lock = AsyncReadWriteLock(lock_file, timeout=5.0, blocking=False, is_singleton=False, executor=executor)
    assert (lock.lock_file, lock.timeout) == (lock_file, pytest.approx(5.0))
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
async def test_write_release_lands_on_another_executor_thread(lock_file: str) -> None:
    executor = ThreadPoolExecutor(max_workers=2)
    lock = AsyncReadWriteLock(lock_file, is_singleton=False, executor=executor)
    await lock.acquire_write()
    started, gate = threading.Event(), threading.Event()

    def park() -> None:
        started.set()
        gate.wait(_TASK_WAIT)

    # The pool's only thread ran the acquire; parking it there forces the release onto a second worker.
    parked = asyncio.get_running_loop().run_in_executor(executor, park)
    await asyncio.to_thread(started.wait, _TASK_WAIT)
    try:
        await lock.release()
    finally:
        gate.set()
        await parked
    async with lock.write_lock(timeout=_TASK_WAIT):
        pass
    await lock.close()
    executor.shutdown()


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
    await AsyncReadWriteLock(lock_file, is_singleton=False, executor=executor).close()
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
    make_ctx = getattr(lock, f"{mode}_lock")
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


@pytest.mark.asyncio
async def test_tasks_sharing_a_lock_take_the_write_lock_one_at_a_time(lock_file: str) -> None:
    assert await writers_peak_inside(AsyncReadWriteLock(lock_file, is_singleton=False)) == 1


async def writers_peak_inside(lock: AsyncReadWriteLock) -> int:
    inside = peak = 0

    async def write() -> None:
        nonlocal inside, peak
        async with lock.write_lock(timeout=_TASK_WAIT):
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


async def _try_write(lock: AsyncReadWriteLock) -> bool:
    try:
        await lock.acquire_write(blocking=False)
    except Timeout:
        return False
    await lock.release()
    return True


@pytest.mark.asyncio
async def test_force_release_drops_the_callers_whole_nest(lock_file: str) -> None:
    lock = AsyncReadWriteLock(lock_file, is_singleton=False)
    await lock.acquire_write()
    await lock.acquire_write()
    await lock.release(force=True)
    entered = await asyncio.create_task(_try_write(lock))
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
    writer = asyncio.create_task(_write_once(lock))
    await asyncio.sleep(0)  # the writer registers as waiting before its first suspension
    with pytest.raises(Timeout):
        await asyncio.create_task(lock.acquire_read(blocking=False))
    await lock.release()
    await asyncio.wait_for(writer, _TASK_WAIT)
    await lock.close()


async def _write_once(lock: AsyncReadWriteLock) -> None:
    async with lock.write_lock(timeout=_TASK_WAIT):
        pass


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
                await asyncio.sleep(0)
                with guard:
                    inside -= 1

    threads = [threading.Thread(target=asyncio.run, args=(write_repeatedly(),), daemon=True) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(_TASK_WAIT)

    assert ([thread.is_alive() for thread in threads], peak) == ([False, False], 1)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", [pytest.param("read", id="read"), pytest.param("write", id="write")])
@pytest.mark.parametrize("shared", [pytest.param(False, id="sqlite"), pytest.param(True, id="task-owner")])
@ACQUIRE_SETTINGS
async def test_async_acquire_respects_settings(
    async_locks: tuple[AsyncReadWriteLock, AsyncReadWriteLock],
    mode: Literal["read", "write"],
    call_timeout: float | None,
    minimum: float,
    *,
    call_blocking: bool | None,
) -> None:
    contender: Final[AsyncReadWriteLock] = async_locks[1]
    acquire: Final = getattr(contender, f"acquire_{mode}")
    async with async_locks[0].write_lock():
        started: Final[float] = time.perf_counter()
        with pytest.raises(Timeout):
            await asyncio.create_task(acquire(timeout=call_timeout, blocking=call_blocking))
        assert minimum <= time.perf_counter() - started < 5


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", [pytest.param("read", id="read"), pytest.param("write", id="write")])
@pytest.mark.parametrize("shared", [pytest.param(False, id="sqlite"), pytest.param(True, id="task-owner")])
@pytest.mark.parametrize(
    ("instance_timeout", "instance_blocking", "minimum"),
    [
        pytest.param(0.2, True, 0.1, id="instance-timeout"),
        pytest.param(30, False, 0, id="instance-nonblocking"),
    ],
)
async def test_async_acquire_without_arguments_uses_instance_settings(
    async_locks: tuple[AsyncReadWriteLock, AsyncReadWriteLock],
    mode: Literal["read", "write"],
    minimum: float,
) -> None:
    contender: Final[AsyncReadWriteLock] = async_locks[1]
    acquire: Final = getattr(contender, f"acquire_{mode}")
    async with async_locks[0].write_lock():
        started: Final[float] = time.perf_counter()
        with pytest.raises(Timeout):
            await asyncio.create_task(acquire())
        assert minimum <= time.perf_counter() - started < 5


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", [pytest.param("read", id="read"), pytest.param("write", id="write")])
@INVALID_TIMEOUTS
async def test_async_instance_timeout_rejects_invalid_value(
    lock_file: str, mode: Literal["read", "write"], timeout: float, message: str
) -> None:
    lock: Final = AsyncReadWriteLock(lock_file, timeout=timeout, is_singleton=False)
    acquire: Final = getattr(lock, f"acquire_{mode}")
    try:
        with pytest.raises(ValueError, match=f"^{re.escape(message)}$"):
            await asyncio.create_task(acquire())
    finally:
        await lock.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", [pytest.param("read", id="read"), pytest.param("write", id="write")])
@INVALID_TIMEOUTS
async def test_async_call_timeout_rejects_invalid_value(
    lock_file: str, mode: Literal["read", "write"], timeout: float, message: str
) -> None:
    lock: Final = AsyncReadWriteLock(lock_file, is_singleton=False)
    acquire: Final = getattr(lock, f"acquire_{mode}")
    try:
        with pytest.raises(ValueError, match=f"^{re.escape(message)}$"):
            await asyncio.create_task(acquire(timeout=timeout))
    finally:
        await lock.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", [pytest.param("read", id="read"), pytest.param("write", id="write")])
@INVALID_TIMEOUTS
async def test_async_contended_acquire_rejects_invalid_timeout(
    lock_file: str, mode: Literal["read", "write"], timeout: float, message: str
) -> None:
    lock: Final = AsyncReadWriteLock(lock_file, is_singleton=False)
    acquire: Final = getattr(lock, f"acquire_{mode}")
    try:
        async with lock.write_lock(timeout=0):
            with pytest.raises(ValueError, match=f"^{re.escape(message)}$"):
                await asyncio.create_task(acquire(timeout=timeout))
    finally:
        await lock.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", [pytest.param("read", id="read"), pytest.param("write", id="write")])
@INVALID_TIMEOUTS
async def test_async_contended_instance_timeout_rejects_invalid_value(
    lock_file: str, mode: Literal["read", "write"], timeout: float, message: str
) -> None:
    lock: Final = AsyncReadWriteLock(lock_file, timeout=timeout, is_singleton=False)
    acquire: Final = getattr(lock, f"acquire_{mode}")
    try:
        async with lock.write_lock(timeout=0):
            with pytest.raises(ValueError, match=f"^{re.escape(message)}$"):
                await asyncio.create_task(acquire())
    finally:
        await lock.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", [pytest.param("read", id="read"), pytest.param("write", id="write")])
@pytest.mark.parametrize("blocking", [pytest.param("false", id="str"), pytest.param(0, id="int")])
async def test_async_acquire_rejects_a_blocking_that_is_not_a_bool(
    lock_file: str, mode: Literal["read", "write"], blocking: str | int
) -> None:
    lock: Final = AsyncReadWriteLock(lock_file, is_singleton=False)
    acquire: Final = getattr(lock, f"acquire_{mode}")
    try:
        with pytest.raises(TypeError, match="blocking must be a bool"):
            await asyncio.create_task(acquire(blocking=blocking))
    finally:
        await lock.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", [pytest.param("read", id="read"), pytest.param("write", id="write")])
@pytest.mark.parametrize(
    "timeout",
    [pytest.param(-2, id="integer"), pytest.param(float("inf"), id="infinite"), pytest.param(float("nan"), id="nan")],
)
async def test_async_nonblocking_instance_ignores_invalid_timeout(
    lock_file: str, mode: Literal["read", "write"], timeout: float
) -> None:
    lock: Final = AsyncReadWriteLock(lock_file, timeout=timeout, blocking=False, is_singleton=False)
    acquire: Final = getattr(lock, f"acquire_{mode}")
    try:
        async with await acquire():
            with pytest.raises(Timeout):
                await asyncio.create_task(lock.acquire_write(timeout=timeout, blocking=False))
    finally:
        await lock.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", [pytest.param("read", id="read"), pytest.param("write", id="write")])
@pytest.mark.parametrize(
    "timeout",
    [pytest.param(-2, id="integer"), pytest.param(float("inf"), id="infinite"), pytest.param(float("nan"), id="nan")],
)
async def test_async_nonblocking_call_ignores_invalid_timeout(
    lock_file: str, mode: Literal["read", "write"], timeout: float
) -> None:
    lock: Final = AsyncReadWriteLock(lock_file, is_singleton=False)
    acquire: Final = getattr(lock, f"acquire_{mode}")
    try:
        async with await acquire(timeout=timeout, blocking=False):
            with pytest.raises(Timeout):
                await asyncio.create_task(lock.acquire_write(timeout=timeout, blocking=False))
    finally:
        await lock.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", [pytest.param("read", id="read"), pytest.param("write", id="write")])
async def test_async_acquire_accepts_infinite_timeout(lock_file: str, mode: Literal["read", "write"]) -> None:
    lock: Final = AsyncReadWriteLock(lock_file, is_singleton=False)
    acquire: Final = getattr(lock, f"acquire_{mode}")
    try:
        async with await acquire(timeout=float("inf")):
            with pytest.raises(Timeout):
                await asyncio.create_task(lock.acquire_write(blocking=False))
    finally:
        await lock.close()


@pytest_asyncio.fixture(loop_scope="function")  # Join shutdown workers before later fork tests.
async def async_locks(
    tmp_path: Path, instance_timeout: float, *, instance_blocking: bool, shared: bool
) -> AsyncIterator[tuple[AsyncReadWriteLock, AsyncReadWriteLock]]:
    holder: Final[AsyncReadWriteLock] = AsyncReadWriteLock(
        tmp_path / "a", timeout=instance_timeout, blocking=instance_blocking, is_singleton=False
    )
    contender: Final[AsyncReadWriteLock] = (
        holder
        if shared
        else AsyncReadWriteLock(
            tmp_path / "a", timeout=instance_timeout, blocking=instance_blocking, is_singleton=False
        )
    )
    try:
        yield holder, contender
    finally:
        await holder.close()
        await contender.close()


@pytest.mark.asyncio
async def test_async_read_after_a_sync_close_of_the_shared_singleton_raises(lock_file: str) -> None:
    lock: Final = AsyncReadWriteLock(lock_file)
    await lock.acquire_read()
    ReadWriteLock(lock_file).close()

    with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
        await asyncio.create_task(lock.acquire_read())
    with pytest.raises(RuntimeError, match="not held by this task"):
        await lock.release()
    await lock.close()


@pytest.mark.parametrize(
    "sync_end",
    [
        pytest.param(ReadWriteLock.close, id="close"),
        pytest.param(lambda sync: sync.release(force=True), id="release"),
    ],
)
@pytest.mark.asyncio
async def test_async_close_after_a_sync_end_of_the_shared_singleton_returns(
    lock_file: str, sync_end: Callable[[ReadWriteLock], object]
) -> None:
    lock: Final = AsyncReadWriteLock(lock_file)
    await lock.acquire_read()
    sync: Final = ReadWriteLock(lock_file)
    sync_end(sync)

    await lock.close()

    assert _writer_gets_in(lock_file)


def _writer_gets_in(lock_file: str) -> bool:
    contender: Final = ReadWriteLock(lock_file, is_singleton=False)
    try:
        contender.acquire_write(blocking=False)
    except Timeout:
        return False
    finally:
        contender.close()
    return True


@pytest.mark.asyncio
async def test_async_release_after_a_sync_forced_release_raises_not_held_by_this_task(lock_file: str) -> None:
    lock: Final = AsyncReadWriteLock(lock_file)
    await lock.acquire_read()
    ReadWriteLock(lock_file).release(force=True)

    with pytest.raises(RuntimeError, match="not held by this task"):
        await lock.release()
    await lock.close()


@pytest.mark.asyncio
async def test_async_read_after_a_sync_forced_release_takes_the_lock_again(lock_file: str) -> None:
    lock: Final = AsyncReadWriteLock(lock_file)
    await lock.acquire_read()
    ReadWriteLock(lock_file).release(force=True)

    writer_got_in = await asyncio.create_task(_read_and_probe(lock))

    assert not writer_got_in
    await lock.close()


async def _read_and_probe(lock: AsyncReadWriteLock) -> bool:
    async with lock.read_lock(timeout=_TASK_WAIT):
        return _writer_gets_in(lock.lock_file)


@pytest.mark.parametrize(
    "sync_end",
    [
        pytest.param(ReadWriteLock.close, id="close"),
        pytest.param(lambda sync: sync.release(force=True), id="release"),
    ],
)
@pytest.mark.asyncio
async def test_async_read_after_an_interrupted_sync_forced_release_takes_the_lock_again(
    lock_file: str, mocker: MockerFixture, sync_end: Callable[[ReadWriteLock], object]
) -> None:
    lock: Final = AsyncReadWriteLock(lock_file)
    await lock.acquire_read()
    mocker.patch.object(
        ReadWriteLock, "_clear_lock_state", new=interrupt_once(ReadWriteLock._clear_lock_state, before=True)
    )
    sync: Final = ReadWriteLock(lock_file)
    with pytest.raises(Interrupt):
        sync_end(sync)

    writer_got_in = await asyncio.create_task(_read_and_probe(lock))

    assert not writer_got_in
    await lock.close()


@pytest.mark.asyncio
async def test_async_read_treats_a_level_whose_connection_closed_meanwhile_as_gone(
    lock_file: str, mocker: MockerFixture
) -> None:
    lock: Final = AsyncReadWriteLock(lock_file)
    await lock.acquire_read()
    real_ended: Final = ReadWriteLock._transaction_ended
    closed = False

    def close_between_checks(instance: ReadWriteLock) -> bool:
        nonlocal closed
        if not closed:
            closed = True
            raise sqlite3.ProgrammingError
        return real_ended(instance)

    mocker.patch.object(ReadWriteLock, "_transaction_ended", autospec=True, side_effect=close_between_checks)

    writer_got_in = await asyncio.create_task(_read_and_probe(lock))

    assert not writer_got_in
    with pytest.raises(RuntimeError, match="not held by this task"):
        await lock.release()
    await lock.close()


@pytest.mark.parametrize("is_singleton", [pytest.param(True, id="singleton"), pytest.param(False, id="owned")])
@pytest.mark.asyncio
async def test_async_read_arriving_during_close_is_refused(lock_file: str, is_singleton: bool) -> None:
    lock: Final = AsyncReadWriteLock(lock_file, is_singleton=is_singleton)
    await lock.acquire_read()
    arriving = asyncio.create_task(_read_and_probe(lock))  # first runs while close() waits for its backend call

    await lock.close()

    with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
        await arriving


@pytest.mark.asyncio
async def test_async_read_through_another_wrapper_during_close_holds_the_lock(lock_file: str) -> None:
    closing, other = AsyncReadWriteLock(lock_file), AsyncReadWriteLock(lock_file)
    await closing.acquire_read()
    arriving = asyncio.create_task(_read_and_probe(other))

    await closing.close()

    assert not await arriving
    await other.close()


@pytest.mark.parametrize("mode", [pytest.param("read", id="read"), pytest.param("write", id="write")])
@pytest.mark.asyncio
async def test_async_close_keeps_the_hold_taken_through_another_wrapper(
    lock_file: str, mode: Literal["read", "write"]
) -> None:
    holder, closing = AsyncReadWriteLock(lock_file), AsyncReadWriteLock(lock_file)
    await getattr(holder, f"acquire_{mode}")()

    await asyncio.create_task(closing.close())

    assert not _writer_gets_in(lock_file)
    await holder.release()
    assert _writer_gets_in(lock_file)
    await holder.close()


@pytest.mark.asyncio
async def test_async_close_of_the_entering_wrapper_keeps_a_level_taken_through_another(lock_file: str) -> None:
    entering, nested = AsyncReadWriteLock(lock_file), AsyncReadWriteLock(lock_file)
    await entering.acquire_read()
    await nested.acquire_read()

    await entering.close()

    assert not _writer_gets_in(lock_file)
    await nested.release()
    assert _writer_gets_in(lock_file)
    await nested.close()


@pytest.mark.asyncio
async def test_async_close_of_the_nested_wrapper_ends_only_its_own_level(lock_file: str) -> None:
    entering, nested = AsyncReadWriteLock(lock_file), AsyncReadWriteLock(lock_file)
    await entering.acquire_read()
    await nested.acquire_read()

    await nested.close()

    await entering.release()
    assert _writer_gets_in(lock_file)
    await entering.close()


@pytest.mark.asyncio
async def test_async_release_through_each_wrapper_ends_the_hold_after_the_last(lock_file: str) -> None:
    entering, nested = AsyncReadWriteLock(lock_file), AsyncReadWriteLock(lock_file)
    await entering.acquire_read()
    await nested.acquire_read()

    await nested.release()

    assert not _writer_gets_in(lock_file)
    await entering.release()
    assert _writer_gets_in(lock_file)
    await entering.close()
    await nested.close()


@pytest.mark.asyncio
async def test_async_forced_release_through_a_wrapper_without_a_level_releases_nothing(lock_file: str) -> None:
    holder, other = AsyncReadWriteLock(lock_file), AsyncReadWriteLock(lock_file)
    await holder.acquire_read()

    await other.release(force=True)

    assert not _writer_gets_in(lock_file)
    await holder.release()
    assert _writer_gets_in(lock_file)
    await holder.close()
    await other.close()


@pytest.mark.asyncio
async def test_async_release_through_a_wrapper_without_a_level_raises(lock_file: str) -> None:
    holder, other = AsyncReadWriteLock(lock_file), AsyncReadWriteLock(lock_file)
    await holder.acquire_read()

    with pytest.raises(RuntimeError, match="through this wrapper"):
        await other.release()

    assert not _writer_gets_in(lock_file)
    await holder.release()
    await holder.close()
    await other.close()


@pytest.mark.asyncio
async def test_async_close_keeps_a_sync_reader_of_the_shared_singleton(lock_file: str) -> None:
    lock: Final = AsyncReadWriteLock(lock_file)
    with ReadWriteLock(lock_file).read_lock():
        async with lock.read_lock():
            pass
        await lock.close()

        assert not _writer_gets_in(lock_file)


@pytest.mark.asyncio
async def test_async_close_of_a_singleton_wrapper_ends_the_holds_taken_through_it(lock_file: str) -> None:
    lock: Final = AsyncReadWriteLock(lock_file)
    await lock.acquire_write()

    await lock.close()

    assert _writer_gets_in(lock_file)
    with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
        await lock.acquire_read()


@pytest.mark.asyncio
async def test_async_close_waits_out_an_acquire_in_flight_even_when_canceled(lock_file: str) -> None:
    holder: Final = ReadWriteLock(lock_file, is_singleton=False)
    holder.acquire_write()
    lock: Final = AsyncReadWriteLock(lock_file, is_singleton=False)
    acquiring = asyncio.create_task(lock.acquire_write(timeout=_TASK_WAIT))
    await asyncio.sleep(0)  # the acquire is now waiting in the backend
    closing = asyncio.create_task(lock.close())
    await asyncio.sleep(0)
    closing.cancel()
    await asyncio.sleep(0)
    holder.release()

    await acquiring
    with pytest.raises(asyncio.CancelledError):
        await closing

    assert _writer_gets_in(lock_file)
    holder.close()


@pytest.mark.asyncio
async def test_async_close_that_fails_before_any_acquire_leaves_the_wrapper_closed(
    lock_file: str, mocker: MockerFixture
) -> None:
    lock: Final = AsyncReadWriteLock(lock_file, is_singleton=False)
    mocker.patch.object(ReadWriteLock, "close", autospec=True, side_effect=sqlite3.OperationalError("close failed"))
    with pytest.raises(sqlite3.OperationalError, match="close failed"):
        await lock.close()

    with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
        await lock.acquire_read()


@pytest.mark.asyncio
async def test_async_reads_giving_up_behind_a_writer_leave_nothing_behind(lock_file: str) -> None:
    lock: Final = AsyncReadWriteLock(lock_file, is_singleton=False)
    await lock.acquire_write()
    await asyncio.create_task(_give_up_reading(lock))
    before: Final = _live_asyncio_futures()
    give_ups: Final = 200

    for _ in range(give_ups):
        await asyncio.create_task(_give_up_reading(lock))

    # The interpreter and the test session keep a handful of futures that come and go. A leak keeps one per give-up,
    # so it shows as growth near give_ups, far past that noise.
    assert _live_asyncio_futures() - before < give_ups // 4
    await lock.release()
    await lock.close()


async def _give_up_reading(lock: AsyncReadWriteLock) -> None:
    with pytest.raises(Timeout):
        await lock.acquire_read(timeout=0.001)


def _live_asyncio_futures() -> int:
    gc.collect()
    return sum(issubclass(type(candidate), asyncio.Future) for candidate in gc.get_objects())
