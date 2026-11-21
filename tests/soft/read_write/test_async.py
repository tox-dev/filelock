from __future__ import annotations

import asyncio
import os
import re
from concurrent.futures import ThreadPoolExecutor
from contextlib import AsyncExitStack, nullcontext
from typing import TYPE_CHECKING, Final, Literal

import pytest

from filelock import AsyncSoftReadWriteLock, Timeout

from .async_helpers import make_async_lock, members, writer_gets_in

if TYPE_CHECKING:
    from collections.abc import Callable
    from contextlib import AbstractAsyncContextManager
    from pathlib import Path

    from pytest_mock import MockerFixture


pytestmark: Final[list[pytest.MarkDecorator]] = [
    pytest.mark.requires_hard_links,
    pytest.mark.usefixtures("clear_singletons"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", [pytest.param("write", id="write"), pytest.param("read", id="read")])
async def test_async_lock_context_manager(tmp_path: Path, mode: Literal["read", "write"]) -> None:
    lock = make_async_lock(tmp_path)
    try:
        async with getattr(lock, f"{mode}_lock")(timeout=2):
            assert lock.generation is not None
        assert lock.generation is None
    finally:
        await lock.close()


@pytest.mark.asyncio
async def test_async_exposes_configuration(tmp_path: Path) -> None:
    lock = make_async_lock(tmp_path, timeout=5, blocking=False)
    try:
        assert lock.lock_file.endswith("foo.lock")
        assert lock.timeout == 5
        assert lock.blocking is False
        assert lock.loop is None
        assert isinstance(lock.executor, ThreadPoolExecutor)
        assert lock.generation is None
        assert lock.compromise is None
    finally:
        await lock.close()


@pytest.mark.asyncio
async def test_async_raw_acquire_release_round_trip(tmp_path: Path) -> None:
    lock = make_async_lock(tmp_path)
    try:
        proxy = await lock.acquire_write(timeout=2)
        async with proxy:
            assert lock.generation is not None
        proxy = await lock.acquire_read(timeout=2)
        async with proxy:
            pass
    finally:
        await lock.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", [pytest.param("read", id="read"), pytest.param("write", id="write")])
@pytest.mark.parametrize(
    ("timeout", "message"),
    [
        pytest.param(-2, "timeout must be a non-negative number or -1, not -2.0", id="integer"),
        pytest.param(-0.5, "timeout must be a non-negative number or -1, not -0.5", id="fraction"),
        pytest.param(float("-inf"), "timeout must be a non-negative number or -1, not -inf", id="negative-infinite"),
        pytest.param(float("nan"), "timeout must be a number of seconds, not nan", id="nan"),
    ],
)
@pytest.mark.parametrize(
    "form",
    [
        pytest.param(lambda timeout: (timeout, {}), id="instance"),
        pytest.param(lambda timeout: (-1, {"timeout": timeout}), id="call"),
    ],
)
@pytest.mark.parametrize(
    "holder",
    [
        pytest.param(lambda lock: lock.write_lock(timeout=0), id="contended"),
        pytest.param(lambda _lock: nullcontext(), id="free"),
    ],
)
async def test_async_acquire_rejects_invalid_timeout(
    tmp_path: Path,
    mode: Literal["read", "write"],
    timeout: float,
    message: str,
    form: Callable[[float], tuple[float, dict[str, float]]],
    holder: Callable[[AsyncSoftReadWriteLock], AbstractAsyncContextManager[None]],
) -> None:
    lock_timeout, call_kwargs = form(timeout)
    lock: Final[AsyncSoftReadWriteLock] = make_async_lock(tmp_path, timeout=lock_timeout)
    acquire = getattr(lock, f"acquire_{mode}")
    try:
        async with AsyncExitStack() as stack:
            await stack.enter_async_context(holder(lock))
            with pytest.raises(ValueError, match=f"^{re.escape(message)}$"):
                await asyncio.create_task(acquire(**call_kwargs))
    finally:
        await lock.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", [pytest.param("read", id="read"), pytest.param("write", id="write")])
@pytest.mark.parametrize(
    "timeout",
    [pytest.param(-2, id="integer"), pytest.param(float("inf"), id="infinite"), pytest.param(float("nan"), id="nan")],
)
@pytest.mark.parametrize(
    "form",
    [
        pytest.param(lambda timeout: (timeout, False, {}), id="instance"),
        pytest.param(lambda timeout: (timeout, True, {"timeout": timeout, "blocking": False}), id="call"),
    ],
)
async def test_async_nonblocking_ignores_invalid_timeout(
    tmp_path: Path,
    mode: Literal["read", "write"],
    timeout: float,
    form: Callable[[float], tuple[float, bool, dict[str, float | bool]]],
) -> None:
    lock_timeout, lock_blocking, call_kwargs = form(timeout)
    lock: Final[AsyncSoftReadWriteLock] = make_async_lock(tmp_path, timeout=lock_timeout, blocking=lock_blocking)
    acquire = getattr(lock, f"acquire_{mode}")
    try:
        async with await acquire(**call_kwargs):
            with pytest.raises(Timeout):
                await asyncio.create_task(lock.acquire_write(timeout=timeout, blocking=False))
    finally:
        await lock.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", [pytest.param("read", id="read"), pytest.param("write", id="write")])
async def test_async_acquire_accepts_infinite_timeout(tmp_path: Path, mode: Literal["read", "write"]) -> None:
    lock: Final[AsyncSoftReadWriteLock] = make_async_lock(tmp_path)
    acquire = getattr(lock, f"acquire_{mode}")
    try:
        async with await acquire(timeout=float("inf")):
            with pytest.raises(Timeout):
                await asyncio.create_task(lock.acquire_write(blocking=False))
    finally:
        await lock.close()


@pytest.mark.asyncio
async def test_async_release_force(tmp_path: Path) -> None:
    lock = make_async_lock(tmp_path)
    try:
        await lock.acquire_read(timeout=2)
        await lock.release(force=True)
        assert lock.generation is None
        assert await asyncio.to_thread(writer_gets_in, tmp_path)
    finally:
        await lock.close()


@pytest.mark.asyncio
async def test_async_release_and_close_skip_when_pid_differs(tmp_path: Path, mocker: MockerFixture) -> None:
    # A pid that no longer matches the creator's must leave the parent's still-held lock untouched.
    lock = make_async_lock(tmp_path)
    await lock.acquire_write(timeout=2)
    try:
        mocker.patch("filelock._soft._read_write._async.os.getpid", return_value=os.getpid() + 1, autospec=True)
        await lock.release(force=True)
        await lock.close()
        mocker.stopall()
        assert not await asyncio.to_thread(writer_gets_in, tmp_path)
        await lock.release()
    finally:
        mocker.stopall()
        await lock.close()


@pytest.mark.asyncio
async def test_async_leaked_singleton_is_closed_on_teardown(tmp_path: Path) -> None:
    # The heartbeat keeps the weakly cached singleton reachable until teardown.
    path = tmp_path / "singleton.lock"
    lock = AsyncSoftReadWriteLock(str(path), heartbeat_interval=0.5, stale_threshold=1.5, poll_interval=0.02)
    await lock.acquire_write(timeout=2)
    assert len(members(tmp_path, "singleton.lock")) == 2


@pytest.mark.timeout(0)  # The per-handoff wait bounds stalls; batch runtime is not a lock guarantee.
@pytest.mark.asyncio
async def test_async_tasks_sharing_a_lock_take_the_write_lock_one_at_a_time(tmp_path: Path) -> None:
    lock: Final[AsyncSoftReadWriteLock] = make_async_lock(tmp_path)
    progress: Final[asyncio.Queue[None]] = asyncio.Queue()
    inside = peak = 0

    async def write() -> None:
        nonlocal inside, peak
        async with lock.write_lock():
            inside += 1
            peak = max(peak, inside)
            await asyncio.sleep(0)
            inside -= 1
        progress.put_nowait(None)

    writers = [asyncio.create_task(write()) for _ in range(10)]
    # A shared deadline can expire during ten slow disk flushes on Windows.
    for _ in writers:
        await asyncio.wait_for(progress.get(), 10)
    await asyncio.gather(*writers)
    await lock.close()

    assert peak == 1


@pytest.mark.asyncio
async def test_async_tasks_sharing_a_lock_hold_the_read_lock_together(tmp_path: Path) -> None:
    lock = make_async_lock(tmp_path)
    readers = 0
    both_inside = asyncio.Event()

    async def read() -> None:
        nonlocal readers
        async with lock.read_lock(timeout=10):
            readers += 1
            if readers == 2:
                both_inside.set()
            await asyncio.wait_for(both_inside.wait(), 10)

    await asyncio.gather(read(), read())
    await lock.close()

    assert readers == 2


@pytest.mark.asyncio
async def test_async_release_from_a_task_that_does_not_hold_raises(tmp_path: Path) -> None:
    lock = make_async_lock(tmp_path)
    await lock.acquire_write(timeout=5)
    with pytest.raises(RuntimeError, match="not held by this task"):
        await asyncio.create_task(lock.release())
    await lock.release()
    await lock.close()


@pytest.mark.asyncio
async def test_async_writer_times_out_behind_reader(tmp_path: Path) -> None:
    reader = make_async_lock(tmp_path)
    await reader.acquire_read(timeout=2)
    try:
        writer = make_async_lock(tmp_path)
        try:
            with pytest.raises(Timeout):
                await writer.acquire_write(timeout=0.3)
        finally:
            await writer.close()
    finally:
        await reader.release()
        await reader.close()
