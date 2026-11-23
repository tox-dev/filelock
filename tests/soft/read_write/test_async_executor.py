from __future__ import annotations

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING, Final

import pytest

from filelock import SoftReadWriteLock

from .async_helpers import TASK_WAIT, BackendGate, make_async_lock, writer_gets_in

if TYPE_CHECKING:
    from pathlib import Path

    from pytest_mock import MockerFixture


pytestmark: Final[list[pytest.MarkDecorator]] = [
    pytest.mark.requires_hard_links,
    pytest.mark.usefixtures("clear_singletons"),
]


@pytest.mark.asyncio
async def test_async_custom_loop_and_executor(tmp_path: Path) -> None:
    with ThreadPoolExecutor(max_workers=1) as executor:
        loop = asyncio.get_running_loop()
        lock = make_async_lock(tmp_path, loop=loop, executor=executor)
        try:
            assert lock.loop is loop
            assert lock.executor is executor
            async with lock.write_lock(timeout=2):
                pass
        finally:
            await lock.close()
        assert await loop.run_in_executor(executor, lambda: True)


@pytest.mark.asyncio
async def test_async_owned_executor_stops_after_close(tmp_path: Path) -> None:
    lock = make_async_lock(tmp_path, is_singleton=True)
    executor = lock.executor
    worker = await asyncio.wrap_future(executor.submit(threading.current_thread))
    resume = threading.Event()
    executor.submit(resume.wait, TASK_WAIT)
    closing = asyncio.create_task(lock.close())
    try:
        await asyncio.sleep(0)
        assert not closing.done()
    finally:
        resume.set()
        await closing

    assert not worker.is_alive()
    with pytest.raises(RuntimeError, match="shutdown"):
        executor.submit(lambda: None)


def test_async_close_after_default_executor_shutdown(tmp_path: Path) -> None:
    async def run() -> None:
        lock = make_async_lock(tmp_path)
        async with lock.write_lock(timeout=2):
            pass
        await asyncio.get_running_loop().shutdown_default_executor()
        await lock.close()

        with pytest.raises(RuntimeError, match="shutdown"):
            lock.executor.submit(lambda: None)

    asyncio.run(run())


def test_async_default_executor_wait_does_not_block_another_release(tmp_path: Path, mocker: MockerFixture) -> None:
    async def run() -> None:
        with ThreadPoolExecutor(max_workers=1) as worker:
            asyncio.get_running_loop().set_default_executor(worker)
            holder = make_async_lock(tmp_path, "private.lock")
            waiter = make_async_lock(tmp_path, "shared.lock")
            holding = asyncio.Event()
            release_requested = asyncio.Event()

            async def hold() -> None:
                await holder.acquire_write(timeout=2)
                holding.set()
                await release_requested.wait()
                await holder.release()

            holder_task = asyncio.create_task(hold())
            await holding.wait()
            gate = BackendGate(mocker, "_acquire_level_of", SoftReadWriteLock._acquire_level_of)

            async def wait() -> None:
                await waiter.acquire_write(timeout=5)
                await waiter.release()

            waiting = asyncio.create_task(wait())
            await gate.started.wait()
            release_requested.set()
            try:
                await holder_task
            finally:
                gate.resume()
                await holder_task
                await waiting
                await holder.close()
                await waiter.close()

    asyncio.run(run())


@pytest.mark.asyncio
async def test_async_write_release_lands_on_another_executor_thread(tmp_path: Path) -> None:
    with ThreadPoolExecutor(max_workers=2) as executor:
        lock = make_async_lock(tmp_path, executor=executor)
        await lock.acquire_write(timeout=2)
        started, gate = threading.Event(), threading.Event()

        def park() -> None:
            started.set()
            gate.wait(5)

        # Parking the first worker forces release onto another thread.
        parked = asyncio.get_running_loop().run_in_executor(executor, park)
        await asyncio.to_thread(started.wait, 5)
        try:
            await lock.release()
        finally:
            gate.set()
            await parked
        assert lock.generation is None
        assert await asyncio.to_thread(writer_gets_in, tmp_path)
        await lock.close()
