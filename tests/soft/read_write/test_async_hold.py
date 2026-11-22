from __future__ import annotations

import asyncio
import threading
from typing import TYPE_CHECKING, Final, Literal

import pytest

from filelock import AsyncSoftReadWriteLock, SoftReadWriteLock, Timeout
from filelock._soft._read_write._generations import GenerationLog, Snapshot
from filelock._soft._read_write._storage import OsFiles
from tests.capability_marks import XFAIL_WITHOUT_COROUTINE_CANCELLATION

from .async_helpers import TASK_WAIT, BackendGate, make_async_lock, make_sync, read_and_probe, writer_gets_in

if TYPE_CHECKING:
    from pathlib import Path

    from pytest_mock import MockerFixture


pytestmark: Final[list[pytest.MarkDecorator]] = [
    pytest.mark.requires_hard_links,
    pytest.mark.usefixtures("clear_singletons"),
]


@pytest.mark.asyncio
async def test_async_soft_read_through_another_wrapper_during_close_holds_the_lock(tmp_path: Path) -> None:
    closing, other = make_async_lock(tmp_path, is_singleton=True), make_async_lock(tmp_path, is_singleton=True)
    await closing.acquire_read(timeout=TASK_WAIT)
    arriving = asyncio.create_task(read_and_probe(other))

    await closing.close()

    assert not await arriving
    await other.close()


@pytest.mark.parametrize("mode", [pytest.param("read", id="read"), pytest.param("write", id="write")])
@pytest.mark.asyncio
async def test_async_soft_close_keeps_the_hold_taken_through_another_wrapper(
    tmp_path: Path, mode: Literal["read", "write"]
) -> None:
    holder, closing = make_async_lock(tmp_path, is_singleton=True), make_async_lock(tmp_path, is_singleton=True)
    await getattr(holder, f"acquire_{mode}")(timeout=TASK_WAIT)

    await asyncio.create_task(closing.close())

    assert not await asyncio.to_thread(writer_gets_in, tmp_path)
    await holder.release()
    assert await asyncio.to_thread(writer_gets_in, tmp_path)
    await holder.close()


@pytest.mark.asyncio
async def test_async_soft_close_of_the_entering_wrapper_keeps_a_level_taken_through_another(tmp_path: Path) -> None:
    entering, nested = make_async_lock(tmp_path, is_singleton=True), make_async_lock(tmp_path, is_singleton=True)
    await entering.acquire_read(timeout=TASK_WAIT)
    await nested.acquire_read(timeout=TASK_WAIT)

    await entering.close()

    assert not await asyncio.to_thread(writer_gets_in, tmp_path)
    await nested.release()
    assert await asyncio.to_thread(writer_gets_in, tmp_path)
    await nested.close()


@pytest.mark.asyncio
async def test_async_soft_close_of_the_nested_wrapper_ends_only_its_own_level(tmp_path: Path) -> None:
    entering, nested = make_async_lock(tmp_path, is_singleton=True), make_async_lock(tmp_path, is_singleton=True)
    await entering.acquire_read(timeout=TASK_WAIT)
    await nested.acquire_read(timeout=TASK_WAIT)

    await nested.close()

    await entering.release()
    assert await asyncio.to_thread(writer_gets_in, tmp_path)
    await entering.close()


@pytest.mark.asyncio
async def test_async_soft_release_through_each_wrapper_ends_the_hold_after_the_last(tmp_path: Path) -> None:
    entering, nested = make_async_lock(tmp_path, is_singleton=True), make_async_lock(tmp_path, is_singleton=True)
    await entering.acquire_read(timeout=TASK_WAIT)
    await nested.acquire_read(timeout=TASK_WAIT)

    await nested.release()

    assert not await asyncio.to_thread(writer_gets_in, tmp_path)
    await entering.release()
    assert await asyncio.to_thread(writer_gets_in, tmp_path)
    await entering.close()
    await nested.close()


@pytest.mark.asyncio
async def test_async_soft_forced_release_through_a_wrapper_without_a_level_releases_nothing(tmp_path: Path) -> None:
    holder, other = make_async_lock(tmp_path, is_singleton=True), make_async_lock(tmp_path, is_singleton=True)
    await holder.acquire_read(timeout=TASK_WAIT)

    await other.release(force=True)

    assert not await asyncio.to_thread(writer_gets_in, tmp_path)
    await holder.release()
    assert await asyncio.to_thread(writer_gets_in, tmp_path)
    await holder.close()
    await other.close()


@pytest.mark.asyncio
async def test_async_soft_release_through_a_wrapper_without_a_level_raises(tmp_path: Path) -> None:
    holder, other = make_async_lock(tmp_path, is_singleton=True), make_async_lock(tmp_path, is_singleton=True)
    await holder.acquire_read(timeout=TASK_WAIT)

    with pytest.raises(RuntimeError, match="through this wrapper"):
        await other.release()

    assert not await asyncio.to_thread(writer_gets_in, tmp_path)
    await holder.release()
    await holder.close()
    await other.close()


@pytest.mark.asyncio
async def test_async_soft_close_of_a_singleton_wrapper_ends_the_holds_taken_through_it(tmp_path: Path) -> None:
    lock: Final[AsyncSoftReadWriteLock] = make_async_lock(tmp_path, is_singleton=True)
    await lock.acquire_write(timeout=TASK_WAIT)

    await lock.close()
    await lock.close()

    assert await asyncio.to_thread(writer_gets_in, tmp_path)
    with pytest.raises(RuntimeError, match="has been closed"):
        await lock.acquire_read(timeout=TASK_WAIT)


@pytest.mark.usefixtures("failing_release")
@pytest.mark.asyncio
async def test_async_soft_read_after_a_failed_release_waits_for_another_writer(tmp_path: Path) -> None:
    lock: Final[AsyncSoftReadWriteLock] = make_async_lock(tmp_path)
    await lock.acquire_read(timeout=5)
    with pytest.raises(OSError, match="No space left"):
        await lock.release()
    writer: Final[SoftReadWriteLock] = make_sync(tmp_path)
    await asyncio.to_thread(writer.acquire_write, 5)

    with pytest.raises(Timeout):
        await asyncio.create_task(lock.acquire_read(blocking=False))
    writer.close()
    await lock.close()


@pytest.mark.usefixtures("failing_release")
@pytest.mark.asyncio
async def test_async_soft_release_retried_after_the_hold_was_dropped_raises(tmp_path: Path) -> None:
    lock: Final[AsyncSoftReadWriteLock] = make_async_lock(tmp_path)
    await lock.acquire_write(timeout=5)
    with pytest.raises(OSError, match="No space left"):
        await lock.release()

    with pytest.raises(RuntimeError, match="not held by this task"):
        await lock.release()
    await lock.close()


@pytest.mark.asyncio
async def test_async_soft_release_leaves_a_sync_reader_of_the_same_singleton_holding(tmp_path: Path) -> None:
    async_lock: Final[AsyncSoftReadWriteLock] = AsyncSoftReadWriteLock(str(tmp_path / "foo.lock"))
    with SoftReadWriteLock(str(tmp_path / "foo.lock")).read_lock():
        async with async_lock.read_lock(timeout=5):
            pass

        assert not await asyncio.to_thread(writer_gets_in, tmp_path)
    await async_lock.close()


@pytest.mark.asyncio
@XFAIL_WITHOUT_COROUTINE_CANCELLATION
async def test_async_soft_canceled_acquire_leaves_a_sync_reader_of_the_same_singleton_holding(
    tmp_path: Path, mocker: MockerFixture
) -> None:
    async_lock: Final[AsyncSoftReadWriteLock] = AsyncSoftReadWriteLock(str(tmp_path / "foo.lock"))
    with SoftReadWriteLock(str(tmp_path / "foo.lock")).read_lock():
        gate = BackendGate(mocker, "_acquire_level_of", SoftReadWriteLock._acquire_level_of)
        acquiring = asyncio.create_task(async_lock.acquire_read(timeout=5))
        await gate.started.wait()
        acquiring.cancel()
        gate.resume()
        with pytest.raises(asyncio.CancelledError):
            await acquiring

        assert not await asyncio.to_thread(writer_gets_in, tmp_path)
    await async_lock.close()


@pytest.mark.asyncio
async def test_async_soft_read_joining_a_lost_hold_raises(tmp_path: Path) -> None:
    lost = threading.Event()
    lock: Final[AsyncSoftReadWriteLock] = make_async_lock(
        tmp_path, heartbeat_interval=0.05, stale_threshold=TASK_WAIT, on_compromise=lambda _compromise: lost.set()
    )
    await lock.acquire_read(timeout=TASK_WAIT)
    _evict_every_holder(tmp_path)
    assert await asyncio.to_thread(lost.wait, TASK_WAIT)

    with pytest.raises(RuntimeError, match="hold was lost"):
        await asyncio.create_task(lock.acquire_read(timeout=1))
    await lock.release()
    await lock.close()


def _evict_every_holder(tmp_path: Path) -> None:
    lock_file = str(tmp_path / "foo.lock")
    log = GenerationLog(OsFiles(lock_file), lock_file, f"{lock_file}.rw")
    assert log.commit(Snapshot(generation=log.latest().generation + 1, writer=None, readers=frozenset()))
