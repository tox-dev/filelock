from __future__ import annotations

import gc
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING, Literal

import pytest

pytest.importorskip("sqlite3")

import sqlite3

from filelock import AsyncReadWriteLock, ReadWriteLock, Timeout

if TYPE_CHECKING:
    from collections.abc import Generator
    from pathlib import Path


@pytest.fixture(autouse=True)
def _clear_singleton_cache() -> Generator[None]:
    ReadWriteLock._instances.clear()
    yield
    for ref in list(ReadWriteLock._instances.valuerefs()):
        if (lock := ref()) is not None:
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
        assert lock._lock._lock_level == 1
        assert lock._lock._current_mode == mode
    assert lock._lock._lock_level == 0
    assert lock._lock._current_mode is None


@pytest.mark.parametrize("mode", [pytest.param("read", id="read"), pytest.param("write", id="write")])
@pytest.mark.asyncio
async def test_reentrant(lock_file: str, mode: Literal["read", "write"]) -> None:
    lock = AsyncReadWriteLock(lock_file, is_singleton=False)
    acquire = lock.acquire_read if mode == "read" else lock.acquire_write
    await acquire()
    await acquire()
    assert lock._lock._lock_level == 2
    await lock.release()
    assert lock._lock._lock_level == 1
    assert lock._lock._current_mode == mode
    await lock.release()
    assert lock._lock._lock_level == 0


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


@pytest.mark.parametrize("mode", [pytest.param("read", id="read"), pytest.param("write", id="write")])
@pytest.mark.asyncio
async def test_non_blocking_conflict(lock_file: str, mode: Literal["read", "write"]) -> None:
    holder = ReadWriteLock(lock_file, is_singleton=False)
    (holder.acquire_write if mode == "read" else holder.acquire_read)()
    try:
        lock = AsyncReadWriteLock(lock_file, is_singleton=False)
        with pytest.raises(Timeout):
            await (lock.acquire_read if mode == "read" else lock.acquire_write)(blocking=False)
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
    finally:
        holder.release()


@pytest.mark.asyncio
async def test_release_unheld_raises(lock_file: str) -> None:
    lock = AsyncReadWriteLock(lock_file, is_singleton=False)
    with pytest.raises(RuntimeError, match="not held"):
        await lock.release()


@pytest.mark.asyncio
async def test_release_force(lock_file: str) -> None:
    lock = AsyncReadWriteLock(lock_file, is_singleton=False)
    await lock.acquire_write()
    await lock.acquire_write()
    assert lock._lock._lock_level == 2
    await lock.release(force=True)
    assert lock._lock._lock_level == 0
    assert lock._lock._current_mode is None


@pytest.mark.asyncio
async def test_release_force_unheld_is_noop(lock_file: str) -> None:
    lock = AsyncReadWriteLock(lock_file, is_singleton=False)
    await lock.release(force=True)


@pytest.mark.asyncio
async def test_close(lock_file: str) -> None:
    lock = AsyncReadWriteLock(lock_file, is_singleton=False)
    await lock.acquire_write()
    assert lock._lock._lock_level == 1
    await lock.close()
    assert lock._lock._lock_level == 0
    with pytest.raises(sqlite3.ProgrammingError, match="Cannot operate on a closed database"):
        lock._lock._con.execute("SELECT 1;")


@pytest.mark.asyncio
async def test_close_on_unheld_lock(lock_file: str) -> None:
    lock = AsyncReadWriteLock(lock_file, is_singleton=False)
    await lock.close()
    with pytest.raises(sqlite3.ProgrammingError, match="Cannot operate on a closed database"):
        lock._lock._con.execute("SELECT 1;")


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
        assert lock._lock._current_mode == "read"
    assert lock._lock._lock_level == 0
    executor.shutdown(wait=False)


@pytest.mark.asyncio
async def test_close_shuts_down_owned_executor(lock_file: str) -> None:
    lock = AsyncReadWriteLock(lock_file, is_singleton=False)
    assert lock._owns_executor is True
    executor = lock.executor
    await lock.close()
    with pytest.raises(RuntimeError):
        executor.submit(int)


@pytest.mark.asyncio
async def test_close_keeps_provided_executor_open(lock_file: str) -> None:
    executor = ThreadPoolExecutor(max_workers=1)
    lock = AsyncReadWriteLock(lock_file, is_singleton=False, executor=executor)
    assert lock._owns_executor is False
    await lock.close()
    assert executor.submit(int).result(timeout=5) == 0
    executor.shutdown(wait=False)


def test_del_shuts_down_owned_executor(lock_file: str) -> None:
    lock = AsyncReadWriteLock(lock_file, is_singleton=False)
    executor = lock.executor
    lock._lock.close()  # isolate the executor lifecycle from the sqlite connection
    del lock
    gc.collect()
    with pytest.raises(RuntimeError):
        executor.submit(int)


def test_del_keeps_provided_executor_open(lock_file: str) -> None:
    executor = ThreadPoolExecutor(max_workers=1)
    lock = AsyncReadWriteLock(lock_file, is_singleton=False, executor=executor)
    lock._lock.close()
    del lock
    gc.collect()
    assert executor.submit(int).result(timeout=5) == 0
    executor.shutdown(wait=False)


@pytest.mark.asyncio
async def test_acquire_return_proxy_context_manager(lock_file: str) -> None:
    lock = AsyncReadWriteLock(lock_file, is_singleton=False)
    async with await lock.acquire_read() as ctx:
        assert ctx is lock
        assert lock._lock._lock_level == 1
    assert lock._lock._lock_level == 0


@pytest.mark.parametrize("mode", [pytest.param("read", id="read"), pytest.param("write", id="write")])
@pytest.mark.asyncio
async def test_nested_context_managers(lock_file: str, mode: Literal["read", "write"]) -> None:
    lock = AsyncReadWriteLock(lock_file, is_singleton=False)
    make_ctx = lock.read_lock if mode == "read" else lock.write_lock
    async with make_ctx():
        assert lock._lock._lock_level == 1
        async with make_ctx():
            assert lock._lock._lock_level == 2
        assert lock._lock._lock_level == 1
    assert lock._lock._lock_level == 0


@pytest.mark.asyncio
async def test_context_manager_uses_instance_defaults(lock_file: str) -> None:
    lock = AsyncReadWriteLock(lock_file, timeout=3.0, blocking=True, is_singleton=False)
    async with lock.read_lock():
        assert lock._lock._current_mode == "read"
    async with lock.write_lock():
        assert lock._lock._current_mode == "write"


@pytest.mark.asyncio
async def test_sequential_mode_switch(lock_file: str) -> None:
    lock = AsyncReadWriteLock(lock_file, is_singleton=False)
    async with lock.read_lock():
        pass
    async with lock.write_lock():
        pass
    async with lock.read_lock():
        pass
