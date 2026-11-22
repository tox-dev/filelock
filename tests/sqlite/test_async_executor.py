from __future__ import annotations

import asyncio
import gc
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING, Final

import pytest

from tests.capability_marks import (
    NEEDS_COLLECTED_FINALIZATION,
)

pytest.importorskip("sqlite3")

import sqlite3

from filelock import AsyncReadWriteLock
from tests.sqlite.async_helpers import TASK_WAIT, assert_mode_held, can_write

if TYPE_CHECKING:
    from pytest_mock import MockerFixture


pytestmark: Final[pytest.MarkDecorator] = pytest.mark.usefixtures("clear_sqlite_singletons")


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
        gate.wait(TASK_WAIT)

    # The pool's only thread ran the acquire; parking it there forces the release onto a second worker.
    parked = asyncio.get_running_loop().run_in_executor(executor, park)
    await asyncio.to_thread(started.wait, TASK_WAIT)
    try:
        await lock.release()
    finally:
        gate.set()
        await parked
    assert await can_write(lock)
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


@pytest.mark.parametrize("shut_down", [pytest.param(False, id="available"), pytest.param(True, id="shut-down")])
def test_close_leaves_the_default_executor_idle(lock_file: str, *, shut_down: bool) -> None:
    async def run() -> None:
        started = threading.Event()
        loop = asyncio.get_running_loop()
        loop.set_default_executor(ThreadPoolExecutor(max_workers=1, initializer=started.set))
        lock = AsyncReadWriteLock(lock_file, is_singleton=False)
        worker = await asyncio.wrap_future(lock.executor.submit(threading.current_thread))
        if shut_down:
            await loop.shutdown_default_executor()
        await lock.close()

        assert not worker.is_alive()
        assert not started.is_set()

    asyncio.run(run())


@pytest.mark.asyncio
async def test_closing_an_unused_singleton_starts_no_worker(lock_file: str, mocker: MockerFixture) -> None:
    lock = AsyncReadWriteLock(lock_file)
    started = mocker.spy(threading.Thread, "start")
    await lock.close()
    started.assert_not_called()
    with pytest.raises(RuntimeError, match="shutdown"):
        lock.executor.submit(int)


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
