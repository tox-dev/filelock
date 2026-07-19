from __future__ import annotations

import asyncio
import os
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING

import pytest

from filelock import Timeout
from filelock._soft_rw import AsyncSoftReadWriteLock, SoftReadWriteLock

if TYPE_CHECKING:
    from collections.abc import Generator
    from concurrent.futures import Executor
    from pathlib import Path

    from pytest_mock import MockerFixture


@pytest.fixture(autouse=True)
def _clear_singletons() -> Generator[None]:
    SoftReadWriteLock._instances.clear()
    yield
    for lock in filter(None, (ref() for ref in list(SoftReadWriteLock._instances.valuerefs()))):
        lock.close()
    SoftReadWriteLock._instances.clear()


def _make(
    tmp_path: Path,
    name: str = "foo.lock",
    *,
    timeout: float = -1,
    blocking: bool = True,
    heartbeat_interval: float = 0.1,
    stale_threshold: float = 0.5,
    poll_interval: float = 0.02,
    loop: asyncio.AbstractEventLoop | None = None,
    executor: Executor | None = None,
) -> AsyncSoftReadWriteLock:
    return AsyncSoftReadWriteLock(
        str(tmp_path / name),
        timeout=timeout,
        blocking=blocking,
        is_singleton=False,
        heartbeat_interval=heartbeat_interval,
        stale_threshold=stale_threshold,
        poll_interval=poll_interval,
        loop=loop,
        executor=executor,
    )


@pytest.mark.asyncio
async def test_async_write_lock_context_manager(tmp_path: Path) -> None:
    lock = _make(tmp_path)
    try:
        async with lock.write_lock(timeout=2):
            pass
    finally:
        await lock.close()


@pytest.mark.asyncio
async def test_async_read_lock_context_manager(tmp_path: Path) -> None:
    lock = _make(tmp_path)
    try:
        async with lock.read_lock(timeout=2):
            pass
    finally:
        await lock.close()


@pytest.mark.asyncio
async def test_async_exposes_configuration(tmp_path: Path) -> None:
    lock = _make(tmp_path, timeout=5, blocking=False)
    try:
        assert lock.lock_file.endswith("foo.lock")
        assert lock.timeout == 5
        assert lock.blocking is False
        assert lock.loop is None
        assert lock.executor is None
    finally:
        await lock.close()


@pytest.mark.asyncio
async def test_async_raw_acquire_release_round_trip(tmp_path: Path) -> None:
    lock = _make(tmp_path)
    try:
        proxy = await lock.acquire_write(timeout=2)
        async with proxy:
            pass
        proxy = await lock.acquire_read(timeout=2)
        async with proxy:
            pass
    finally:
        await lock.close()


@pytest.mark.asyncio
async def test_async_custom_loop_and_executor(tmp_path: Path) -> None:
    with ThreadPoolExecutor(max_workers=1) as executor:
        loop = asyncio.get_running_loop()
        lock = _make(tmp_path, loop=loop, executor=executor)
        try:
            assert lock.loop is loop
            assert lock.executor is executor
            async with lock.write_lock(timeout=2):
                pass
        finally:
            await lock.close()


@pytest.mark.asyncio
async def test_async_release_force(tmp_path: Path) -> None:
    lock = _make(tmp_path)
    try:
        await lock.acquire_read(timeout=2)
        await lock.release(force=True)
    finally:
        await lock.close()


@pytest.mark.asyncio
async def test_async_release_and_close_skip_when_pid_differs(tmp_path: Path, mocker: MockerFixture) -> None:
    # A pid that no longer matches the creator's must leave the parent's still-held lock untouched.
    lock = _make(tmp_path)
    await lock.acquire_write(timeout=2)
    try:
        mocker.patch("filelock._soft_rw._async.os.getpid", return_value=os.getpid() + 1)
        await lock.release(force=True)
        await lock.close()
        mocker.stopall()
        await lock.release()
    finally:
        mocker.stopall()
        await lock.close()


@pytest.mark.asyncio
async def test_async_leaked_singleton_is_closed_on_teardown(tmp_path: Path) -> None:
    # A live heartbeat thread keeps the singleton reachable, so the autouse teardown finds and closes it.
    path = tmp_path / "singleton.lock"
    lock = AsyncSoftReadWriteLock(str(path), heartbeat_interval=0.5, stale_threshold=1.5, poll_interval=0.02)
    await lock.acquire_write(timeout=2)
    assert path.with_name("singleton.lock.write").exists()


@pytest.mark.asyncio
async def test_async_writer_times_out_behind_reader(tmp_path: Path) -> None:
    reader = _make(tmp_path)
    await reader.acquire_read(timeout=2)
    try:
        writer = _make(tmp_path)
        try:
            with pytest.raises(Timeout):
                await writer.acquire_write(timeout=0.3)
        finally:
            await writer.close()
    finally:
        await reader.release()
        await reader.close()
