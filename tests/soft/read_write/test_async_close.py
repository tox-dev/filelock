from __future__ import annotations

import asyncio
import functools
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING, Final, Literal

import pytest

from filelock import AsyncSoftReadWriteLock, SoftReadWriteLock
from filelock._soft._read_write._storage import OsFiles
from tests.capability_marks import XFAIL_WITHOUT_COROUTINE_CANCELLATION

from .async_helpers import TASK_WAIT, make_async_lock, make_sync, members, read_and_probe, writer_gets_in

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from pytest_mock import MockerFixture

    from filelock import AcquireReturnProxy

pytestmark: Final[list[pytest.MarkDecorator]] = [
    pytest.mark.requires_hard_links,
    pytest.mark.usefixtures("clear_singletons"),
]


@pytest.mark.asyncio
async def test_async_soft_read_after_a_sync_close_of_the_shared_singleton_raises(tmp_path: Path) -> None:
    lock: Final[AsyncSoftReadWriteLock] = make_async_lock(tmp_path, is_singleton=True)
    await lock.acquire_read(timeout=TASK_WAIT)
    await asyncio.to_thread(SoftReadWriteLock(str(tmp_path / "foo.lock")).close)

    with pytest.raises(RuntimeError, match="has been closed"):
        await asyncio.create_task(lock.acquire_read(timeout=TASK_WAIT))
    with pytest.raises(RuntimeError, match="not held by this task"):
        await lock.release()
    await lock.close()


@pytest.mark.parametrize(
    "sync_end",
    [
        pytest.param(SoftReadWriteLock.close, id="close"),
        pytest.param(functools.partial(SoftReadWriteLock.release, force=True), id="release"),
    ],
)
@pytest.mark.asyncio
async def test_async_soft_close_after_a_sync_end_of_the_shared_singleton_returns(
    tmp_path: Path, sync_end: Callable[[SoftReadWriteLock], object]
) -> None:
    lock: Final[AsyncSoftReadWriteLock] = make_async_lock(tmp_path, is_singleton=True)
    await lock.acquire_read(timeout=TASK_WAIT)
    sync: Final[SoftReadWriteLock] = SoftReadWriteLock(str(tmp_path / "foo.lock"))
    await asyncio.to_thread(sync_end, sync)

    await lock.close()

    assert members(tmp_path) == []


@pytest.mark.asyncio
async def test_async_soft_release_after_a_sync_forced_release_raises_not_held_by_this_task(tmp_path: Path) -> None:
    lock: Final[AsyncSoftReadWriteLock] = make_async_lock(tmp_path, is_singleton=True)
    await lock.acquire_read(timeout=TASK_WAIT)
    await asyncio.to_thread(SoftReadWriteLock(str(tmp_path / "foo.lock")).release, force=True)

    with pytest.raises(RuntimeError, match="not held by this task"):
        await lock.release()
    await lock.close()


@pytest.mark.parametrize("is_singleton", [pytest.param(True, id="singleton"), pytest.param(False, id="owned")])
@pytest.mark.asyncio
async def test_async_soft_read_arriving_during_close_is_refused(tmp_path: Path, is_singleton: bool) -> None:
    lock: Final[AsyncSoftReadWriteLock] = make_async_lock(tmp_path, is_singleton=is_singleton)
    await lock.acquire_read(timeout=TASK_WAIT)
    arriving = asyncio.create_task(read_and_probe(lock))  # first runs while close() waits for its backend call

    await lock.close()

    with pytest.raises(RuntimeError, match="has been closed"):
        await arriving


@pytest.mark.parametrize("is_singleton", [pytest.param(True, id="singleton"), pytest.param(False, id="owned")])
@pytest.mark.parametrize("mode", [pytest.param("read", id="read"), pytest.param("write", id="write")])
@pytest.mark.asyncio
async def test_async_soft_acquire_waiting_in_backend_during_close_is_refused(
    tmp_path: Path, is_singleton: bool, mode: Literal["read", "write"], mocker: MockerFixture
) -> None:
    holder: Final[SoftReadWriteLock] = make_sync(tmp_path)
    holder.acquire_write()
    lock: Final[AsyncSoftReadWriteLock] = make_async_lock(tmp_path, is_singleton=is_singleton)
    granted, resume = asyncio.Event(), asyncio.Event()
    run_acquire = lock._run_acquire

    async def gated_acquire(acquire: functools.partial[AcquireReturnProxy]) -> None:
        await run_acquire(acquire)
        granted.set()
        await resume.wait()

    mocker.patch.object(lock, "_run_acquire", autospec=True, side_effect=gated_acquire)
    acquiring = asyncio.create_task(getattr(lock, f"acquire_{mode}")(timeout=TASK_WAIT))
    holder.release()
    await granted.wait()
    closing = asyncio.create_task(lock.close())
    await asyncio.sleep(0)
    resume.set()

    with pytest.raises(RuntimeError, match="has been closed"):
        await acquiring
    await closing

    assert writer_gets_in(tmp_path)
    holder.close()


@pytest.mark.asyncio
async def test_async_soft_acquire_keeps_its_hold_when_close_fails(tmp_path: Path, mocker: MockerFixture) -> None:
    holder: Final[SoftReadWriteLock] = make_sync(tmp_path)
    holder.acquire_write()
    lock: Final[AsyncSoftReadWriteLock] = make_async_lock(tmp_path)
    granted, resume, entered, finish = (asyncio.Event() for _ in range(4))
    run_acquire = lock._run_acquire

    async def gated_acquire(acquire: functools.partial[AcquireReturnProxy]) -> None:
        await run_acquire(acquire)
        granted.set()
        await resume.wait()

    mocker.patch.object(lock, "_run_acquire", autospec=True, side_effect=gated_acquire)

    async def work() -> None:
        async with lock.write_lock(timeout=TASK_WAIT):
            entered.set()
            await finish.wait()

    acquiring = asyncio.create_task(work())
    holder.release()
    await granted.wait()
    mocker.patch.object(lock._lock, "close", autospec=True, side_effect=OSError("close failed"))
    closing = asyncio.create_task(lock.close())
    await asyncio.sleep(0)
    resume.set()

    with pytest.raises(OSError, match="close failed"):
        await closing
    await entered.wait()
    assert not writer_gets_in(tmp_path)

    finish.set()
    await acquiring
    assert writer_gets_in(tmp_path)
    mocker.stopall()
    await lock.close()
    holder.close()


@pytest.mark.parametrize(
    ("release_error", "expected_error", "available"),
    [
        pytest.param(None, asyncio.CancelledError, True, id="release-succeeds"),
        pytest.param(OSError("release failed"), OSError, False, id="release-fails"),
    ],
)
@pytest.mark.asyncio
async def test_async_soft_cancelled_acquire_after_failed_close_reconciles_hold(
    tmp_path: Path,
    mocker: MockerFixture,
    release_error: OSError | None,
    expected_error: type[BaseException],
    available: bool,
) -> None:
    holder: Final[SoftReadWriteLock] = make_sync(tmp_path)
    holder.acquire_write()
    lock: Final[AsyncSoftReadWriteLock] = make_async_lock(tmp_path)
    granted, resume = asyncio.Event(), asyncio.Event()
    close_started, finish_close = threading.Event(), threading.Event()
    run_acquire = lock._run_acquire

    async def gated_acquire(acquire: functools.partial[AcquireReturnProxy]) -> None:
        await run_acquire(acquire)
        granted.set()
        await resume.wait()

    def fail_close() -> None:
        close_started.set()
        assert finish_close.wait(5)
        msg = "close failed"
        raise OSError(msg)

    mocker.patch.object(lock, "_leave_backend", autospec=True, wraps=lock._leave_backend, side_effect=release_error)
    mocker.patch.object(lock, "_run_acquire", autospec=True, side_effect=gated_acquire)
    acquiring = asyncio.create_task(lock.acquire_write(timeout=TASK_WAIT))
    holder.release()
    await granted.wait()
    mocker.patch.object(lock._lock, "close", autospec=True, side_effect=fail_close)
    closing = asyncio.create_task(lock.close())
    await asyncio.sleep(0)
    resume.set()
    assert await asyncio.to_thread(close_started.wait, 5)
    acquiring.cancel()
    finish_close.set()

    with pytest.raises(OSError, match="close failed"):
        await closing
    with pytest.raises(expected_error) as info:
        await acquiring
    assert isinstance(info.value.__context__, asyncio.CancelledError) is not available
    assert writer_gets_in(tmp_path) is available
    mocker.stopall()
    await lock.close()
    assert writer_gets_in(tmp_path)
    holder.close()


@pytest.mark.asyncio
async def test_async_soft_cancelled_acquire_during_close_leaves_no_hold(tmp_path: Path, mocker: MockerFixture) -> None:
    holder: Final[SoftReadWriteLock] = make_sync(tmp_path)
    holder.acquire_write()
    lock: Final[AsyncSoftReadWriteLock] = make_async_lock(tmp_path)
    granted, resume = asyncio.Event(), asyncio.Event()
    close_started, finish_close = threading.Event(), threading.Event()
    run_acquire, close_backend = lock._run_acquire, lock._lock.close

    async def gated_acquire(acquire: functools.partial[AcquireReturnProxy]) -> None:
        await run_acquire(acquire)
        granted.set()
        await resume.wait()

    def paused_close() -> None:
        close_started.set()
        assert finish_close.wait(5)
        close_backend()

    mocker.patch.object(lock, "_run_acquire", autospec=True, side_effect=gated_acquire)
    acquiring = asyncio.create_task(lock.acquire_write(timeout=TASK_WAIT))
    holder.release()
    await granted.wait()
    mocker.patch.object(lock._lock, "close", autospec=True, side_effect=paused_close)
    closing = asyncio.create_task(lock.close())
    await asyncio.sleep(0)
    resume.set()
    assert await asyncio.to_thread(close_started.wait, 5)
    acquiring.cancel()
    finish_close.set()

    await closing
    with pytest.raises(asyncio.CancelledError):
        await acquiring
    assert writer_gets_in(tmp_path)
    holder.close()


@pytest.mark.asyncio
@XFAIL_WITHOUT_COROUTINE_CANCELLATION
async def test_async_soft_close_cancellation_refuses_later_acquires(tmp_path: Path) -> None:
    started, resume = threading.Event(), threading.Event()

    def park() -> None:
        started.set()
        resume.wait(5)

    with ThreadPoolExecutor(max_workers=1) as worker:
        lock: Final[AsyncSoftReadWriteLock] = make_async_lock(tmp_path, executor=worker)
        await lock.acquire_read(timeout=5)
        # The parked worker lets cancellation arrive before close runs.
        parked = asyncio.get_running_loop().run_in_executor(worker, park)
        await asyncio.to_thread(started.wait, 5)
        closing = asyncio.create_task(lock.close())
        await asyncio.sleep(0)
        closing.cancel()
        resume.set()
        await parked
        with pytest.raises(asyncio.CancelledError):
            await closing

        with pytest.raises(RuntimeError, match="has been closed"):
            await asyncio.create_task(lock.acquire_read(timeout=5))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("held_mode", "waiting_mode"),
    [pytest.param("write", "read", id="reader"), pytest.param("read", "write", id="writer")],
)
async def test_async_soft_close_wakes_its_table_waiter(
    tmp_path: Path, held_mode: Literal["read", "write"], waiting_mode: Literal["read", "write"]
) -> None:
    holder, closing = make_async_lock(tmp_path, is_singleton=True), make_async_lock(tmp_path, is_singleton=True)
    await getattr(holder, f"acquire_{held_mode}")()
    acquiring = asyncio.create_task(getattr(closing, f"acquire_{waiting_mode}")())
    try:
        assert not (await asyncio.wait((acquiring,), timeout=0.05))[0]

        await closing.close()

        assert acquiring in (await asyncio.wait((acquiring,), timeout=1))[0]
        with pytest.raises(RuntimeError, match="has been closed"):
            await acquiring
    finally:
        await holder.close()
        await asyncio.gather(acquiring, return_exceptions=True)


@pytest.mark.asyncio
async def test_async_soft_close_of_waiting_writer_unblocks_other_readers(tmp_path: Path) -> None:
    holder, closing, reader = (make_async_lock(tmp_path, is_singleton=True) for _ in range(3))
    await holder.acquire_read()
    acquiring_write = asyncio.create_task(closing.acquire_write())
    await asyncio.wait((acquiring_write,), timeout=0.05)
    acquiring_read = asyncio.create_task(reader.acquire_read())
    try:
        assert not acquiring_write.done()
        assert not (await asyncio.wait((acquiring_read,), timeout=0.05))[0]

        await closing.close()

        with pytest.raises(RuntimeError, match="has been closed"):
            await asyncio.wait_for(acquiring_write, 1)
        await asyncio.wait_for(acquiring_read, 1)
        assert not await asyncio.to_thread(writer_gets_in, tmp_path)
    finally:
        await holder.close()
        await reader.close()
        await asyncio.gather(acquiring_write, acquiring_read, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("is_singleton", [False, True])
async def test_async_soft_close_stops_a_pending_acquire(
    tmp_path: Path, is_singleton: bool, mocker: MockerFixture
) -> None:
    holder = make_sync(tmp_path)
    holder.acquire_write(timeout=5)
    lock = make_async_lock(tmp_path, is_singleton=is_singleton)
    started = threading.Event()
    create = OsFiles.create

    def observe_create(
        files: OsFiles,
        path: str,
        data: bytes,
        *,
        durable: bool = False,
    ) -> tuple[int, int]:
        started.set()
        return create(files, path, data, durable=durable)

    mocker.patch.object(OsFiles, "create", autospec=True, side_effect=observe_create)
    acquiring = asyncio.create_task(lock.acquire_write())
    try:
        assert await asyncio.to_thread(started.wait, 5)
        assert not acquiring.done()

        closing = asyncio.create_task(lock.close())
        done, _ = await asyncio.wait((closing,), timeout=1)
        assert closing in done
        await closing
        with pytest.raises(RuntimeError, match="has been closed"):
            await acquiring
    finally:
        holder.close()
        await asyncio.gather(acquiring, return_exceptions=True)
        await lock.close()
