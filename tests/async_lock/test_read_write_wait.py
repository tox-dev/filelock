from __future__ import annotations

import asyncio
import sys
import threading
import time
from contextlib import suppress
from errno import EWOULDBLOCK
from typing import TYPE_CHECKING, Final, Literal, Protocol

import pytest
import pytest_asyncio

pytest.importorskip("sqlite3")


from filelock import AsyncReadWriteLock, AsyncSoftReadWriteLock, ReadWriteLock, SoftReadWriteLock, Timeout
from filelock._core import override
from tests.capability_marks import XFAIL_WITHOUT_COROUTINE_CANCELLATION

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable
    from pathlib import Path

    from pytest_mock import MockerFixture

# A missed deadline must expire before the peer releases its hold.
_HOLD: Final[float] = 3
_DEADLINE: Final[float] = 0.2

pytestmark: Final[list[pytest.MarkDecorator]] = [
    XFAIL_WITHOUT_COROUTINE_CANCELLATION,
    pytest.mark.usefixtures("clear_rw_singletons"),
]


class _Make(Protocol):
    def __call__(
        self, path: Path, *, is_singleton: bool
    ) -> tuple[ReadWriteLock | SoftReadWriteLock, AsyncReadWriteLock | AsyncSoftReadWriteLock]: ...


class _SQLitePair(_Make):
    @override
    def __call__(self, path: Path, *, is_singleton: bool) -> tuple[ReadWriteLock, AsyncReadWriteLock]:
        database = path / "lock.db"
        return ReadWriteLock(database, is_singleton=is_singleton), AsyncReadWriteLock(database)


class _SoftPair(_Make):
    @override
    def __call__(  # pragma: needs hard-link
        self, path: Path, *, is_singleton: bool
    ) -> tuple[SoftReadWriteLock, AsyncSoftReadWriteLock]:
        lock_file = path / "lock"
        return (
            SoftReadWriteLock(
                lock_file, is_singleton=is_singleton, heartbeat_interval=0.1, stale_threshold=0.5, poll_interval=0.02
            ),
            AsyncSoftReadWriteLock(lock_file, heartbeat_interval=0.1, stale_threshold=0.5, poll_interval=0.02),
        )


_FAMILIES: Final[pytest.MarkDecorator] = pytest.mark.parametrize(
    "make",
    [
        pytest.param(_SQLitePair(), id="sqlite"),
        pytest.param(_SoftPair(), id="soft", marks=pytest.mark.requires_hard_links),
    ],
)
_MODES: Final[pytest.MarkDecorator] = pytest.mark.parametrize(
    "mode", [pytest.param("read", id="read"), pytest.param("write", id="write")]
)


@_FAMILIES
@_MODES
@pytest.mark.parametrize(
    "timeout", [pytest.param(-1, id="no-lock-timeout"), pytest.param(_HOLD * 2, id="lock-timeout")]
)
@pytest.mark.asyncio
async def test_async_acquire_behind_another_process_honors_the_callers_deadline(
    tmp_path: Path, make: _Make, mode: Literal["read", "write"], timeout: float
) -> None:
    holder, lock = make(tmp_path, is_singleton=False)
    holder.acquire_write()
    ending = asyncio.get_running_loop().call_later(_HOLD, holder.release)

    elapsed = await _acquire_within_deadline(lock, mode, timeout)

    assert elapsed < _HOLD
    ending.cancel()
    holder.release()
    await getattr(lock, f"acquire_{mode}")(_HOLD * 2)
    await lock.release()
    await lock.close()


async def _acquire_within_deadline(
    lock: AsyncReadWriteLock | AsyncSoftReadWriteLock, mode: Literal["read", "write"], timeout: float
) -> float:
    start = time.perf_counter()
    with pytest.raises(TimeoutError) as info:
        async with asyncio.timeout(_DEADLINE):
            await getattr(lock, f"acquire_{mode}")(timeout)
    assert not isinstance(info.value, Timeout)
    return time.perf_counter() - start


@pytest.mark.skipif(sys.platform != "win32", reason="oplocks exist only on Windows")
@pytest.mark.asyncio
async def test_async_acquire_oplocked_database_honors_caller_deadline(
    tmp_path: Path, hold_oplock: Callable[[Path], Callable[[], None]]
) -> None:  # pragma: win32 cover
    database = tmp_path / "lock.db"
    lock = AsyncReadWriteLock(database, is_singleton=False)
    close_holder = hold_oplock(database)
    try:
        elapsed = await _acquire_within_deadline(lock, "write", _HOLD)
        assert elapsed < _HOLD / 2
    finally:
        close_holder()
        await lock.close()


@pytest.mark.asyncio
async def test_refused_database_open_does_not_spin(tmp_path: Path, mocker: MockerFixture) -> None:
    lock = AsyncReadWriteLock(tmp_path / "lock.db")
    mocker.patch(
        "filelock._sqlite._transactions.connect",
        side_effect=BlockingIOError(EWOULDBLOCK, "leased database"),
        autospec=True,
    )
    started, cpu = time.monotonic(), time.process_time()

    with pytest.raises(Timeout):
        await lock.acquire_write(timeout=0.2)

    elapsed = time.monotonic() - started
    assert 0.15 <= elapsed < 1
    assert time.process_time() - cpu < elapsed / 2
    await lock.close()


@pytest.mark.asyncio
async def test_refused_database_open_obeys_async_cancellation(tmp_path: Path, mocker: MockerFixture) -> None:
    lock = AsyncReadWriteLock(tmp_path / "lock.db")
    mocker.patch.object(threading, "active_count", return_value=1, autospec=True)
    mocker.patch(
        "filelock._sqlite._transactions.connect",
        side_effect=BlockingIOError(EWOULDBLOCK, "leased database"),
        autospec=True,
    )

    elapsed = await _acquire_within_deadline(lock, "write", _HOLD)

    assert elapsed < 1
    await lock.close()


@_FAMILIES
@pytest.mark.asyncio
async def test_async_acquire_behind_a_sync_acquire_of_the_same_instance_honors_the_callers_deadline(
    tmp_path: Path, make: _Make
) -> None:
    holder, lock = make(tmp_path, is_singleton=False)
    shared, _ = make(tmp_path, is_singleton=True)
    holder.acquire_write()

    # A thread waiting in the shared instance's acquire keeps that instance busy for the task too.
    def write_once() -> None:
        with shared.write_lock(timeout=_HOLD * 2):
            pass

    waiting = threading.Thread(target=write_once)
    waiting.start()
    ending = asyncio.get_running_loop().call_later(_HOLD, holder.release)

    elapsed = await _acquire_within_deadline(lock, "write", -1)

    assert elapsed < _HOLD
    ending.cancel()
    holder.release()
    await asyncio.to_thread(waiting.join, _HOLD * 2)
    await lock.acquire_write(timeout=_HOLD)
    await lock.release()
    await lock.close()


@_FAMILIES
@pytest.mark.asyncio
async def test_async_acquire_behind_a_sync_acquire_of_the_same_instance_times_out_at_its_own_deadline(
    tmp_path: Path, make: _Make, mocker: MockerFixture
) -> None:
    # Single-threaded acquires can spend the whole deadline in one fork transition.
    mocker.patch.object(threading, "active_count", return_value=1, autospec=True)
    holder, lock = make(tmp_path, is_singleton=False)
    shared, _ = make(tmp_path, is_singleton=True)
    holder.acquire_write()

    def write_once() -> None:
        with shared.write_lock(timeout=_HOLD * 2):
            pass

    waiting = threading.Thread(target=write_once)
    waiting.start()
    ending = asyncio.get_running_loop().call_later(_HOLD, holder.release)
    start = time.perf_counter()

    with pytest.raises(Timeout):
        await lock.acquire_write(_DEADLINE)

    assert time.perf_counter() - start < _HOLD
    ending.cancel()
    holder.release()
    await asyncio.to_thread(waiting.join, _HOLD * 2)
    await lock.close()


@_FAMILIES
@pytest.mark.asyncio
async def test_singleton_close_returns_while_another_wrapper_enters(tmp_path: Path, make: _Make) -> None:
    holder, waiting = make(tmp_path, is_singleton=False)
    shared, idle = make(tmp_path, is_singleton=True)
    with holder.write_lock():
        acquiring = asyncio.create_task(waiting.acquire_write())
        await asyncio.sleep(0)
        closing = asyncio.create_task(idle.close())
        done, _ = await asyncio.wait({closing}, timeout=2)
    try:
        await acquiring
        await closing
        assert closing in done
    finally:
        await waiting.close()
        shared.close()
        holder.close()


@_FAMILIES
@pytest.mark.parametrize(
    "closing_worker",
    [pytest.param("retire", id="backend"), pytest.param("join", id="worker"), pytest.param("retry", id="canceled")],
    indirect=True,
)
@pytest.mark.asyncio
async def test_second_close_waits_for_the_owned_worker(
    closing_worker: tuple[AsyncReadWriteLock | AsyncSoftReadWriteLock, threading.Thread, threading.Event],
) -> None:
    lock, worker, resume = closing_worker

    async def close_on_another_loop() -> None:
        second = asyncio.create_task(lock.close())
        await asyncio.sleep(0)
        try:
            assert not second.done()
        finally:
            resume.set()
            await second
        assert not worker.is_alive()

    await asyncio.to_thread(asyncio.run, close_on_another_loop())


@pytest_asyncio.fixture(loop_scope="function")
async def closing_worker(
    request: pytest.FixtureRequest, tmp_path: Path, make: _Make
) -> AsyncIterator[tuple[AsyncReadWriteLock | AsyncSoftReadWriteLock, threading.Thread, threading.Event]]:
    sync, lock = make(tmp_path, is_singleton=True)
    if request.param == "retire":
        await lock.acquire_write()
    worker = await asyncio.wrap_future(lock.executor.submit(threading.current_thread))
    resume = threading.Event()
    lock.executor.submit(resume.wait, 5)
    closing = asyncio.create_task(lock.close())
    try:
        await asyncio.sleep(0)
        if request.param == "retry":
            closing.cancel()
            with pytest.raises(asyncio.CancelledError):
                await closing
        yield lock, worker, resume
    finally:
        resume.set()
        with suppress(asyncio.CancelledError):
            await closing
        sync.close()
