from __future__ import annotations

import asyncio
import errno
import functools
import os
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import AsyncExitStack
from pathlib import Path
from typing import TYPE_CHECKING, Final, Literal

import pytest

from filelock import Timeout
from filelock._soft_rw import AsyncSoftReadWriteLock, SoftReadWriteLock
from filelock._soft_rw._protocol import GenerationLog, Snapshot
from filelock._soft_rw._storage import OsFiles
from tests.capability_marks import XFAIL_WITHOUT_COROUTINE_CANCELLATION

if TYPE_CHECKING:
    from collections.abc import Callable, Generator
    from concurrent.futures import Executor

    from pytest_mock import MockerFixture

    from filelock._lease import LeaseCompromise

pytestmark = pytest.mark.requires_hard_links

# Bounds every wait on another task, so a lock that never admits it fails the test instead of hanging the suite.
_TASK_WAIT: Final[float] = 10


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
    is_singleton: bool = False,
    on_compromise: Callable[[LeaseCompromise], None] | None = None,
) -> AsyncSoftReadWriteLock:
    return AsyncSoftReadWriteLock(
        str(tmp_path / name),
        timeout=timeout,
        blocking=blocking,
        is_singleton=is_singleton,
        heartbeat_interval=heartbeat_interval,
        stale_threshold=stale_threshold,
        poll_interval=poll_interval,
        loop=loop,
        executor=executor,
        on_compromise=on_compromise,
    )


class _Gate:
    """Pause one call inside the executor thread so a cancellation lands after the backend work started."""

    def __init__(self, mocker: MockerFixture, name: str, *, fail_with: BaseException | None = None) -> None:
        self.started = asyncio.Event()
        # Set once the gated call returns, letting a test distinguish the executor finishing from the caller giving up.
        self.finished = threading.Event()
        self._resume = threading.Event()
        self._loop = asyncio.get_running_loop()
        self._real: Callable[..., object] = getattr(SoftReadWriteLock, name)
        self._pending = True
        mocker.patch.object(SoftReadWriteLock, name, autospec=True, side_effect=self._call(fail_with))

    def _call(self, fail_with: BaseException | None) -> Callable[..., object]:
        def call(lock: SoftReadWriteLock, *args: object, **kwargs: object) -> object:
            if not self._pending:
                return self._real(lock, *args, **kwargs)
            self._pending = False
            self._loop.call_soon_threadsafe(self.started.set)
            assert self._resume.wait(timeout=5)
            try:
                if fail_with is not None:
                    raise fail_with
                return self._real(lock, *args, **kwargs)
            finally:
                self.finished.set()

        return call

    def resume(self) -> None:
        self._resume.set()


def _members(tmp_path: Path, name: str = "foo.lock") -> list[str]:
    lock_file = str(tmp_path / name)
    latest = GenerationLog(OsFiles(lock_file), lock_file, f"{lock_file}.rw").latest()
    holders = tmp_path / f"{name}.rw" / "holders"
    return sorted(latest.members) + (sorted(entry.name for entry in holders.iterdir()) if holders.is_dir() else [])


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", [pytest.param("write", id="write"), pytest.param("read", id="read")])
async def test_async_lock_context_manager(tmp_path: Path, mode: Literal["read", "write"]) -> None:
    lock = _make(tmp_path)
    try:
        context = lock.write_lock if mode == "write" else lock.read_lock
        async with context(timeout=2):
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
        assert lock.generation is None
        assert lock.compromise is None
    finally:
        await lock.close()


@pytest.mark.asyncio
async def test_async_raw_acquire_release_round_trip(tmp_path: Path) -> None:
    lock = _make(tmp_path)
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
@pytest.mark.parametrize("configured", [pytest.param(True, id="instance"), pytest.param(False, id="call")])
@pytest.mark.parametrize("contended", [pytest.param(True, id="contended"), pytest.param(False, id="free")])
async def test_async_acquire_rejects_invalid_timeout(
    tmp_path: Path,
    mode: Literal["read", "write"],
    timeout: float,
    message: str,
    *,
    configured: bool,
    contended: bool,
) -> None:
    lock: Final = _make(tmp_path, timeout=timeout if configured else -1)
    acquire: Final = lock.acquire_read if mode == "read" else lock.acquire_write
    try:
        async with AsyncExitStack() as stack:
            if contended:
                await stack.enter_async_context(lock.write_lock(timeout=0))
            with pytest.raises(ValueError, match=f"^{re.escape(message)}$"):
                await asyncio.create_task(acquire() if configured else acquire(timeout=timeout))
    finally:
        await lock.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", [pytest.param("read", id="read"), pytest.param("write", id="write")])
@pytest.mark.parametrize(
    "timeout",
    [pytest.param(-2, id="integer"), pytest.param(float("inf"), id="infinite"), pytest.param(float("nan"), id="nan")],
)
@pytest.mark.parametrize("configured", [pytest.param(True, id="instance"), pytest.param(False, id="call")])
async def test_async_nonblocking_ignores_invalid_timeout(
    tmp_path: Path, mode: Literal["read", "write"], timeout: float, *, configured: bool
) -> None:
    lock: Final = _make(tmp_path, timeout=timeout, blocking=not configured)
    acquire: Final = lock.acquire_read if mode == "read" else lock.acquire_write
    try:
        async with await (acquire() if configured else acquire(timeout=timeout, blocking=False)):
            with pytest.raises(Timeout):
                await asyncio.create_task(lock.acquire_write(timeout=timeout, blocking=False))
    finally:
        await lock.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", [pytest.param("read", id="read"), pytest.param("write", id="write")])
async def test_async_acquire_accepts_infinite_timeout(tmp_path: Path, mode: Literal["read", "write"]) -> None:
    lock: Final = _make(tmp_path)
    acquire: Final = lock.acquire_read if mode == "read" else lock.acquire_write
    try:
        async with await acquire(timeout=float("inf")):
            with pytest.raises(Timeout):
                await asyncio.create_task(lock.acquire_write(blocking=False))
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
async def test_async_write_release_lands_on_another_executor_thread(tmp_path: Path) -> None:
    with ThreadPoolExecutor(max_workers=2) as executor:
        lock = _make(tmp_path, executor=executor)
        await lock.acquire_write(timeout=2)
        started, gate = threading.Event(), threading.Event()

        def park() -> None:
            started.set()
            gate.wait(5)

        # The pool's only thread ran the acquire; parking it there forces the release onto a second worker.
        parked = asyncio.get_running_loop().run_in_executor(executor, park)
        await asyncio.to_thread(started.wait, 5)
        try:
            await lock.release()
        finally:
            gate.set()
            await parked
        async with lock.write_lock(timeout=2):
            pass
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
    assert len(_members(tmp_path, "singleton.lock")) == 2


@pytest.mark.timeout(0)  # The per-handoff wait bounds stalls; batch runtime is not a lock guarantee.
@pytest.mark.asyncio
async def test_async_tasks_sharing_a_lock_take_the_write_lock_one_at_a_time(tmp_path: Path) -> None:
    lock: Final = _make(tmp_path)
    progress: Final[asyncio.Queue[None]] = asyncio.Queue()
    inside = peak = 0

    async def write() -> None:
        nonlocal inside, peak
        async with lock.write_lock():
            inside += 1
            peak = max(peak, inside)
            await asyncio.sleep(0.001)
            inside -= 1
        progress.put_nowait(None)

    writers: Final = [asyncio.create_task(write()) for _ in range(10)]
    # Each handoff waits on fsync and link, which stall for seconds on Windows CI disks; ten queued writers then
    # overrun one shared deadline without any writer being stuck.
    for _ in writers:
        await asyncio.wait_for(progress.get(), 10)
    await asyncio.gather(*writers)
    await lock.close()

    assert peak == 1


@pytest.mark.asyncio
async def test_async_tasks_sharing_a_lock_hold_the_read_lock_together(tmp_path: Path) -> None:
    lock = _make(tmp_path)
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
    lock = _make(tmp_path)
    await lock.acquire_write(timeout=5)
    with pytest.raises(RuntimeError, match="not held by this task"):
        await asyncio.create_task(lock.release())
    await lock.release()
    await lock.close()


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


@pytest.mark.parametrize("mode", [pytest.param("read", id="read"), pytest.param("write", id="write")])
@pytest.mark.asyncio
@XFAIL_WITHOUT_COROUTINE_CANCELLATION
async def test_async_acquire_cancellation_hands_the_claim_back(
    tmp_path: Path, mocker: MockerFixture, mode: Literal["read", "write"]
) -> None:
    gate = _Gate(mocker, "_acquire_level_of")
    lock = _make(tmp_path)
    acquire = lock.acquire_read if mode == "read" else lock.acquire_write
    task = asyncio.create_task(acquire(timeout=5))
    await gate.started.wait()
    task.cancel("cancel acquire")
    gate.resume()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert gate.finished.wait(timeout=5), "the executor never finished the acquire it was already running"

    assert lock._lock._hold is None
    assert _members(tmp_path) == []
    contender = SoftReadWriteLock(str(tmp_path / "foo.lock"), is_singleton=False, poll_interval=0.02)
    try:
        with contender.write_lock(timeout=5):
            pass
    finally:
        contender.close()
    await lock.close()


@pytest.mark.asyncio
@XFAIL_WITHOUT_COROUTINE_CANCELLATION
async def test_async_acquire_cancellation_surfaces_a_failed_acquire(tmp_path: Path, mocker: MockerFixture) -> None:
    acquire_error = Timeout(str(tmp_path / "foo.lock"))
    gate = _Gate(mocker, "_acquire_level_of", fail_with=acquire_error)
    lock = _make(tmp_path)
    task = asyncio.create_task(lock.acquire_write(timeout=5))
    await gate.started.wait()
    task.cancel("cancel acquire")
    gate.resume()
    with pytest.raises(Timeout) as info:
        await task

    assert info.value is acquire_error
    assert isinstance(acquire_error.__context__, asyncio.CancelledError)
    assert lock._lock._hold is None
    assert _members(tmp_path) == []
    await lock.close()


@pytest.mark.asyncio
@XFAIL_WITHOUT_COROUTINE_CANCELLATION
async def test_async_acquire_cancellation_surfaces_a_failed_release(tmp_path: Path, mocker: MockerFixture) -> None:
    gate = _Gate(mocker, "_acquire_level_of")
    release_error = RuntimeError("release failed")
    mocker.patch.object(SoftReadWriteLock, "_release_level_of", side_effect=release_error)
    lock = _make(tmp_path)
    task = asyncio.create_task(lock.acquire_write(timeout=5))
    await gate.started.wait()
    task.cancel("cancel acquire")
    gate.resume()
    with pytest.raises(RuntimeError, match="release failed") as info:
        await task

    assert info.value is release_error
    assert isinstance(release_error.__context__, asyncio.CancelledError)
    mocker.stopall()
    await lock.close()


@pytest.mark.asyncio
@XFAIL_WITHOUT_COROUTINE_CANCELLATION
async def test_async_release_cancellation_drains_the_release(tmp_path: Path, mocker: MockerFixture) -> None:
    lock = _make(tmp_path)
    gate = _Gate(mocker, "_release_level_of")
    task = asyncio.create_task(_acquire_then_release(lock, "write"))
    await gate.started.wait()
    task.cancel("cancel release")
    # Resume only after the cancellation has had a chance to propagate, so the cancellation reaching the caller
    # before the release finished is a failure rather than a coin flip.
    asyncio.get_running_loop().call_later(0.05, gate.resume)
    with pytest.raises(asyncio.CancelledError):
        await task

    assert gate.finished.is_set(), "the cancellation surfaced while the release was still running"
    assert lock._lock._hold is None
    assert _members(tmp_path) == []
    await lock.close()


@pytest.mark.parametrize("mode", [pytest.param("read", id="read"), pytest.param("write", id="write")])
@pytest.mark.asyncio
@XFAIL_WITHOUT_COROUTINE_CANCELLATION
async def test_async_release_cancellation_hands_the_lock_to_the_next_task(
    tmp_path: Path, mocker: MockerFixture, mode: Literal["read", "write"]
) -> None:
    lock = _make(tmp_path)
    gate = _Gate(mocker, "_release_level_of")
    task = asyncio.create_task(_acquire_then_release(lock, mode))
    await gate.started.wait()
    task.cancel("cancel release")
    gate.resume()
    with pytest.raises(asyncio.CancelledError):
        await task

    await lock.acquire_write(timeout=2)
    peer = SoftReadWriteLock(str(tmp_path / "foo.lock"), is_singleton=False, poll_interval=0.02)
    try:
        with pytest.raises(Timeout):
            peer.acquire_read(blocking=False)
    finally:
        peer.close()
    await lock.release()
    await lock.close()


@pytest.mark.parametrize("fail", [pytest.param(False, id="closed"), pytest.param(True, id="failed")])
@pytest.mark.asyncio
@XFAIL_WITHOUT_COROUTINE_CANCELLATION
async def test_async_close_cancellation_drains_the_close(tmp_path: Path, mocker: MockerFixture, *, fail: bool) -> None:
    lock = _make(tmp_path)
    await lock.acquire_write(timeout=5)
    close_error = RuntimeError("close failed")
    gate = _Gate(mocker, "close", fail_with=close_error if fail else None)
    task = asyncio.create_task(lock.close())
    await gate.started.wait()
    task.cancel("cancel close")
    gate.resume()
    with pytest.raises(RuntimeError if fail else asyncio.CancelledError) as info:
        await task

    assert gate.finished.is_set()
    if fail:
        assert info.value is close_error
        assert isinstance(close_error.__context__, asyncio.CancelledError)
        mocker.stopall()
        await lock.close()
    assert _members(tmp_path) == []


@pytest.mark.asyncio
@XFAIL_WITHOUT_COROUTINE_CANCELLATION
async def test_async_release_cancellation_surfaces_a_failed_release(tmp_path: Path, mocker: MockerFixture) -> None:
    lock = _make(tmp_path)
    release_error = RuntimeError("release failed")
    gate = _Gate(mocker, "_release_level_of", fail_with=release_error)
    task = asyncio.create_task(_acquire_then_release(lock, "write"))
    await gate.started.wait()
    task.cancel("cancel release")
    gate.resume()
    with pytest.raises(RuntimeError, match="release failed") as info:
        await task

    assert info.value is release_error
    assert isinstance(release_error.__context__, asyncio.CancelledError)
    mocker.stopall()
    await lock.close()


async def _acquire_then_release(lock: AsyncSoftReadWriteLock, mode: Literal["read", "write"]) -> None:
    # A hold belongs to the task that took it, so the task whose release gets canceled has to acquire first.
    await (lock.acquire_read if mode == "read" else lock.acquire_write)(timeout=5)
    await lock.release()


@pytest.mark.asyncio
async def test_async_soft_read_after_a_sync_close_of_the_shared_singleton_raises(tmp_path: Path) -> None:
    lock: Final = _make(tmp_path, is_singleton=True)
    await lock.acquire_read(timeout=_TASK_WAIT)
    await asyncio.to_thread(SoftReadWriteLock(str(tmp_path / "foo.lock")).close)

    with pytest.raises(RuntimeError, match="has been closed"):
        await asyncio.create_task(lock.acquire_read(timeout=_TASK_WAIT))
    with pytest.raises(RuntimeError, match="not held by this task"):
        await lock.release()
    await lock.close()


@pytest.mark.parametrize("sync_end", [pytest.param("release", id="release"), pytest.param("close", id="close")])
@pytest.mark.asyncio
async def test_async_soft_close_after_a_sync_end_of_the_shared_singleton_returns(tmp_path: Path, sync_end: str) -> None:
    lock: Final = _make(tmp_path, is_singleton=True)
    await lock.acquire_read(timeout=_TASK_WAIT)
    sync: Final = SoftReadWriteLock(str(tmp_path / "foo.lock"))
    await asyncio.to_thread(sync.close if sync_end == "close" else functools.partial(sync.release, force=True))

    await lock.close()

    assert _members(tmp_path, "foo.lock") == []


@pytest.mark.asyncio
async def test_async_soft_release_after_a_sync_forced_release_raises_not_held_by_this_task(tmp_path: Path) -> None:
    lock: Final = _make(tmp_path, is_singleton=True)
    await lock.acquire_read(timeout=_TASK_WAIT)
    await asyncio.to_thread(SoftReadWriteLock(str(tmp_path / "foo.lock")).release, force=True)

    with pytest.raises(RuntimeError, match="not held by this task"):
        await lock.release()
    await lock.close()


@pytest.mark.parametrize("is_singleton", [pytest.param(True, id="singleton"), pytest.param(False, id="owned")])
@pytest.mark.asyncio
async def test_async_soft_read_arriving_during_close_is_refused(tmp_path: Path, is_singleton: bool) -> None:
    lock: Final = _make(tmp_path, is_singleton=is_singleton)
    await lock.acquire_read(timeout=_TASK_WAIT)
    arriving = asyncio.create_task(_read_and_probe(lock))  # first runs while close() waits for its backend call

    await lock.close()

    with pytest.raises(RuntimeError, match="has been closed"):
        await arriving


@pytest.mark.asyncio
async def test_async_soft_read_through_another_wrapper_during_close_holds_the_lock(tmp_path: Path) -> None:
    closing, other = _make(tmp_path, is_singleton=True), _make(tmp_path, is_singleton=True)
    await closing.acquire_read(timeout=_TASK_WAIT)
    arriving = asyncio.create_task(_read_and_probe(other))

    await closing.close()

    assert not await arriving
    await other.close()


@pytest.mark.parametrize("mode", [pytest.param("read", id="read"), pytest.param("write", id="write")])
@pytest.mark.asyncio
async def test_async_soft_close_keeps_the_hold_taken_through_another_wrapper(
    tmp_path: Path, mode: Literal["read", "write"]
) -> None:
    holder, closing = _make(tmp_path, is_singleton=True), _make(tmp_path, is_singleton=True)
    await (holder.acquire_read if mode == "read" else holder.acquire_write)(timeout=_TASK_WAIT)

    await asyncio.create_task(closing.close())

    assert not await asyncio.to_thread(_writer_gets_in, tmp_path)
    await holder.release()
    assert await asyncio.to_thread(_writer_gets_in, tmp_path)
    await holder.close()


@pytest.mark.asyncio
async def test_async_soft_close_of_the_entering_wrapper_keeps_a_level_taken_through_another(tmp_path: Path) -> None:
    entering, nested = _make(tmp_path, is_singleton=True), _make(tmp_path, is_singleton=True)
    await entering.acquire_read(timeout=_TASK_WAIT)
    await nested.acquire_read(timeout=_TASK_WAIT)

    await entering.close()

    assert not await asyncio.to_thread(_writer_gets_in, tmp_path)
    await nested.release()
    assert await asyncio.to_thread(_writer_gets_in, tmp_path)
    await nested.close()


@pytest.mark.asyncio
async def test_async_soft_close_of_the_nested_wrapper_ends_only_its_own_level(tmp_path: Path) -> None:
    entering, nested = _make(tmp_path, is_singleton=True), _make(tmp_path, is_singleton=True)
    await entering.acquire_read(timeout=_TASK_WAIT)
    await nested.acquire_read(timeout=_TASK_WAIT)

    await nested.close()

    await entering.release()
    assert await asyncio.to_thread(_writer_gets_in, tmp_path)
    await entering.close()


@pytest.mark.asyncio
async def test_async_soft_release_through_each_wrapper_ends_the_hold_after_the_last(tmp_path: Path) -> None:
    entering, nested = _make(tmp_path, is_singleton=True), _make(tmp_path, is_singleton=True)
    await entering.acquire_read(timeout=_TASK_WAIT)
    await nested.acquire_read(timeout=_TASK_WAIT)

    await nested.release()

    assert not await asyncio.to_thread(_writer_gets_in, tmp_path)
    await entering.release()
    assert await asyncio.to_thread(_writer_gets_in, tmp_path)
    await entering.close()
    await nested.close()


@pytest.mark.asyncio
async def test_async_soft_forced_release_through_a_wrapper_without_a_level_releases_nothing(tmp_path: Path) -> None:
    holder, other = _make(tmp_path, is_singleton=True), _make(tmp_path, is_singleton=True)
    await holder.acquire_read(timeout=_TASK_WAIT)

    await other.release(force=True)

    assert not await asyncio.to_thread(_writer_gets_in, tmp_path)
    await holder.release()
    assert await asyncio.to_thread(_writer_gets_in, tmp_path)
    await holder.close()
    await other.close()


@pytest.mark.asyncio
async def test_async_soft_release_through_a_wrapper_without_a_level_raises(tmp_path: Path) -> None:
    holder, other = _make(tmp_path, is_singleton=True), _make(tmp_path, is_singleton=True)
    await holder.acquire_read(timeout=_TASK_WAIT)

    with pytest.raises(RuntimeError, match="through this wrapper"):
        await other.release()

    assert not await asyncio.to_thread(_writer_gets_in, tmp_path)
    await holder.release()
    await holder.close()
    await other.close()


@pytest.mark.asyncio
async def test_async_soft_close_of_a_singleton_wrapper_ends_the_holds_taken_through_it(tmp_path: Path) -> None:
    lock: Final = _make(tmp_path, is_singleton=True)
    await lock.acquire_write(timeout=_TASK_WAIT)

    await lock.close()
    await lock.close()

    assert await asyncio.to_thread(_writer_gets_in, tmp_path)
    with pytest.raises(RuntimeError, match="has been closed"):
        await lock.acquire_read(timeout=_TASK_WAIT)


def _writer_gets_in(tmp_path: Path) -> bool:
    contender: Final = _make_sync(tmp_path)
    try:
        contender.acquire_write(blocking=False)
    except Timeout:
        return False
    finally:
        contender.close()
    return True


def _make_sync(tmp_path: Path) -> SoftReadWriteLock:
    return SoftReadWriteLock(
        str(tmp_path / "foo.lock"), is_singleton=False, heartbeat_interval=0.1, stale_threshold=0.5
    )


async def _read_and_probe(lock: AsyncSoftReadWriteLock) -> bool:
    async with lock.read_lock(timeout=_TASK_WAIT):
        return await asyncio.to_thread(_writer_gets_in, Path(lock.lock_file).parent)


@pytest.mark.usefixtures("failing_release")
@pytest.mark.asyncio
async def test_async_soft_read_after_a_failed_release_waits_for_another_writer(tmp_path: Path) -> None:
    lock: Final = _make(tmp_path)
    await lock.acquire_read(timeout=5)
    with pytest.raises(OSError, match="No space left"):
        await lock.release()
    writer: Final = _make_sync(tmp_path)
    await asyncio.to_thread(writer.acquire_write, 5)

    with pytest.raises(Timeout):
        await asyncio.create_task(lock.acquire_read(blocking=False))
    writer.close()
    await lock.close()


@pytest.mark.usefixtures("failing_release")
@pytest.mark.asyncio
async def test_async_soft_release_retried_after_the_hold_was_dropped_raises(tmp_path: Path) -> None:
    lock: Final = _make(tmp_path)
    await lock.acquire_write(timeout=5)
    with pytest.raises(OSError, match="No space left"):
        await lock.release()

    with pytest.raises(RuntimeError, match="not held by this task"):
        await lock.release()
    await lock.close()


@pytest.fixture
def failing_release(mocker: MockerFixture) -> None:
    # The sync lock drops its hold before the filesystem step that fails, as a full disk does.
    real_release = SoftReadWriteLock._release_level_of

    def release(lock: SoftReadWriteLock, holder: int) -> None:
        real_release(lock, holder)
        raise OSError(errno.ENOSPC, "No space left on device")

    mocker.patch.object(SoftReadWriteLock, "_release_level_of", autospec=True, side_effect=release)


@pytest.mark.asyncio
@XFAIL_WITHOUT_COROUTINE_CANCELLATION
async def test_async_soft_close_cancellation_refuses_later_acquires(tmp_path: Path) -> None:
    started, resume = threading.Event(), threading.Event()

    def park() -> None:
        started.set()
        resume.wait(5)

    with ThreadPoolExecutor(max_workers=1) as worker:
        lock: Final = _make(tmp_path, executor=worker)
        await lock.acquire_read(timeout=5)
        # Parking the only worker keeps close() waiting for its backend call when the cancellation lands.
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
async def test_async_soft_release_leaves_a_sync_reader_of_the_same_singleton_holding(tmp_path: Path) -> None:
    async_lock: Final = AsyncSoftReadWriteLock(str(tmp_path / "foo.lock"))
    with SoftReadWriteLock(str(tmp_path / "foo.lock")).read_lock():
        async with async_lock.read_lock(timeout=5):
            pass

        assert not await asyncio.to_thread(_writer_gets_in, tmp_path)
    await async_lock.close()


@pytest.mark.asyncio
@XFAIL_WITHOUT_COROUTINE_CANCELLATION
async def test_async_soft_canceled_acquire_leaves_a_sync_reader_of_the_same_singleton_holding(
    tmp_path: Path, mocker: MockerFixture
) -> None:
    async_lock: Final = AsyncSoftReadWriteLock(str(tmp_path / "foo.lock"))
    with SoftReadWriteLock(str(tmp_path / "foo.lock")).read_lock():
        gate = _Gate(mocker, "_acquire_level_of")
        acquiring = asyncio.create_task(async_lock.acquire_read(timeout=5))
        await gate.started.wait()
        acquiring.cancel()
        gate.resume()
        with pytest.raises(asyncio.CancelledError):
            await acquiring

        assert not await asyncio.to_thread(_writer_gets_in, tmp_path)
    await async_lock.close()


@pytest.mark.asyncio
async def test_async_soft_read_joining_a_lost_hold_raises(tmp_path: Path) -> None:
    lost = threading.Event()
    lock: Final = _make(
        tmp_path, heartbeat_interval=0.05, stale_threshold=0.2, on_compromise=lambda _compromise: lost.set()
    )
    await lock.acquire_read(timeout=2)
    _evict_every_holder(tmp_path)
    assert await asyncio.to_thread(lost.wait, 10)

    with pytest.raises(RuntimeError, match="hold was lost"):
        await asyncio.create_task(lock.acquire_read(timeout=1))
    await lock.release()
    await lock.close()


def _evict_every_holder(tmp_path: Path) -> None:
    lock_file = str(tmp_path / "foo.lock")
    log = GenerationLog(OsFiles(lock_file), lock_file, f"{lock_file}.rw")
    assert log.commit(Snapshot(generation=log.latest().generation + 1, writer=None, readers=frozenset()))
