from __future__ import annotations

import asyncio
import re
import time
from typing import TYPE_CHECKING, Final, Literal

import pytest
import pytest_asyncio

from tests.read_write_helpers import (
    ACQUIRE_SETTINGS,
    INVALID_TIMEOUTS,
)

pytest.importorskip("sqlite3")


from filelock import AsyncReadWriteLock, Timeout

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path


pytestmark: Final[pytest.MarkDecorator] = pytest.mark.usefixtures("clear_sqlite_singletons")


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
    acquire = getattr(contender, f"acquire_{mode}")
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
    acquire = getattr(contender, f"acquire_{mode}")
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
    lock: Final[AsyncReadWriteLock] = AsyncReadWriteLock(lock_file, timeout=timeout, is_singleton=False)
    acquire = getattr(lock, f"acquire_{mode}")
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
    lock: Final[AsyncReadWriteLock] = AsyncReadWriteLock(lock_file, is_singleton=False)
    acquire = getattr(lock, f"acquire_{mode}")
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
    lock: Final[AsyncReadWriteLock] = AsyncReadWriteLock(lock_file, is_singleton=False)
    acquire = getattr(lock, f"acquire_{mode}")
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
    lock: Final[AsyncReadWriteLock] = AsyncReadWriteLock(lock_file, timeout=timeout, is_singleton=False)
    acquire = getattr(lock, f"acquire_{mode}")
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
    lock: Final[AsyncReadWriteLock] = AsyncReadWriteLock(lock_file, is_singleton=False)
    acquire = getattr(lock, f"acquire_{mode}")
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
    lock: Final[AsyncReadWriteLock] = AsyncReadWriteLock(lock_file, timeout=timeout, blocking=False, is_singleton=False)
    acquire = getattr(lock, f"acquire_{mode}")
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
    lock: Final[AsyncReadWriteLock] = AsyncReadWriteLock(lock_file, is_singleton=False)
    acquire = getattr(lock, f"acquire_{mode}")
    try:
        async with await acquire(timeout=timeout, blocking=False):
            with pytest.raises(Timeout):
                await asyncio.create_task(lock.acquire_write(timeout=timeout, blocking=False))
    finally:
        await lock.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", [pytest.param("read", id="read"), pytest.param("write", id="write")])
async def test_async_acquire_accepts_infinite_timeout(lock_file: str, mode: Literal["read", "write"]) -> None:
    lock: Final[AsyncReadWriteLock] = AsyncReadWriteLock(lock_file, is_singleton=False)
    acquire = getattr(lock, f"acquire_{mode}")
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
