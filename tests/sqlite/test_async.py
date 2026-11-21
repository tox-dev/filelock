from __future__ import annotations

import asyncio
import errno
import os
from typing import TYPE_CHECKING, Final, Literal, NoReturn

import pytest

from tests.capability_marks import (
    NEEDS_FORK,
)
from tests.fork_helpers import exit_child, fork_process
from tests.read_write_helpers import (
    writer_gets_in,
)

pytest.importorskip("sqlite3")


from filelock import AsyncReadWriteLock, FileLock, ReadWriteLock, Timeout
from tests.sqlite.async_helpers import assert_mode_held, can_write

if TYPE_CHECKING:
    from pathlib import Path


pytestmark: Final[pytest.MarkDecorator] = pytest.mark.usefixtures("clear_sqlite_singletons")


@NEEDS_FORK
def test_failed_async_construction_keeps_other_fork_resets(tmp_path: Path) -> None:  # pragma: win32 no cover
    index = tmp_path / "index.db"
    index.symlink_to(tmp_path / "missing.db")
    with pytest.raises(OSError, match="symbolic links") as failed:
        AsyncReadWriteLock(index)
    parent = FileLock(tmp_path / "private.lock", is_singleton=False)
    parent.acquire()

    def check_child() -> NoReturn:
        assert (parent.is_locked, parent.lock_counter) == (False, 0)
        exit_child(0)

    try:
        _, status = os.waitpid(fork_process(check_child), 0)
        assert os.waitstatus_to_exitcode(status) == 0
    finally:
        parent.release()
    assert failed.value.errno == errno.ELOOP


@pytest.mark.asyncio
async def test_async_release_leaves_a_sync_reader_of_the_same_singleton_holding(lock_file: str) -> None:
    async_lock: Final[AsyncReadWriteLock] = AsyncReadWriteLock(lock_file)
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
    assert await can_write(lock)
    await lock.close()


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
        assert not await asyncio.to_thread(writer_gets_in, lock_file)
    async with lock.write_lock():
        assert not await asyncio.to_thread(writer_gets_in, lock_file)
    async with lock.read_lock():
        assert not await asyncio.to_thread(writer_gets_in, lock_file)
    await lock.close()
    assert writer_gets_in(lock_file)
