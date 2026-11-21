from __future__ import annotations

import asyncio
import gc
import subprocess  # ruff:ignore[suspicious-subprocess-import]  # interpreter exit finalizes a held lock
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from itertools import count
from typing import TYPE_CHECKING, Final

import pytest
import pytest_asyncio

from filelock import (
    AsyncFileLock,
    AsyncSoftFileLock,
    BaseAsyncFileLock,
    BaseFileLock,
    FileLock,
    Timeout,
)
from tests.capability_marks import (
    NEEDS_PROMPT_FINALIZATION,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path
    from types import FrameType

    from _typeshed import Unused  # simplint: ignore[private-import-path]  # precise callback type
    from pytest_mock import MockerFixture, MockType


@pytest.mark.parametrize(
    "lock_type",
    [pytest.param(AsyncFileLock, id="async"), pytest.param(AsyncSoftFileLock, id="soft")],
)
def test_del_after_loop_close_does_not_raise(lock_type: type[BaseAsyncFileLock], tmp_path: Path) -> None:
    # __del__ must not call get_running_loop(); it raises RuntimeError when no loop is running
    def _run() -> None:
        lock = lock_type(str(tmp_path / "test.lock"))
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(lock.acquire())
            loop.run_until_complete(lock.release(force=True))
        finally:
            loop.close()
            asyncio.set_event_loop(None)
        del lock
        gc.collect()

    with ThreadPoolExecutor(max_workers=1) as pool:
        pool.submit(_run).result(timeout=10)


@pytest.mark.parametrize("lock_type", [AsyncFileLock, AsyncSoftFileLock])
def test_del_with_stopped_loop_releases_on_that_loop(lock_type: type[BaseAsyncFileLock], tmp_path: Path) -> None:
    loop = asyncio.new_event_loop()
    try:
        lock = lock_type(str(tmp_path / "test.lock"), loop=loop)
        loop.run_until_complete(lock.acquire())
        assert lock.is_locked
        lock.__del__()  # ruff:ignore[unnecessary-dunder-call]  # a finalizer test cannot ride on collection timing
        assert not lock.is_locked
    finally:
        loop.close()


def test_async_del_without_any_loop_returns(tmp_path: Path, mocker: MockerFixture) -> None:
    # GC can finalize a lock with no loop running and none remembered, where releasing is impossible. Raise the lookup
    # rather than rely on no loop running here, which the surrounding tests decide.
    get_running_loop = mocker.patch("asyncio.get_running_loop", side_effect=RuntimeError, autospec=True)
    lock = AsyncSoftFileLock(str(tmp_path / "a"))
    release = mocker.patch.object(lock, "release", autospec=True)
    # Any lock another test dropped reaches the same patched lookup, so only calls from here on count.
    get_running_loop.reset_mock()

    lock.__del__()  # ruff:ignore[unnecessary-dunder-call]  # a finalizer test cannot ride on collection timing

    # An unlocked finalizer asserts nothing on its own; the lookup and the absent release pin the path it took.
    assert (get_running_loop.called, release.called, lock.is_locked) == (True, False, False)


def test_async_del_without_any_loop_releases_a_sync_backend(tmp_path: Path) -> None:
    lock = AsyncSoftFileLock(str(tmp_path / "a"))
    asyncio.run(lock.acquire())

    lock.__del__()  # ruff:ignore[unnecessary-dunder-call]  # a finalizer test cannot ride on collection timing

    assert (lock.is_locked, lock.lock_counter, (tmp_path / "a").exists()) == (False, 0, False)


def test_async_del_schedules_release_on_a_stored_loop_in_another_thread(tmp_path: Path, mocker: MockerFixture) -> None:
    loop = asyncio.new_event_loop()
    runner = threading.Thread(target=loop.run_forever)
    runner.start()
    try:
        lock = AsyncSoftFileLock(str(tmp_path / "a"), loop=loop)
        asyncio.run_coroutine_threadsafe(lock.acquire(), loop).result(timeout=5)
        schedule = mocker.spy(asyncio, "run_coroutine_threadsafe")

        lock.__del__()  # ruff:ignore[unnecessary-dunder-call]  # a finalizer test cannot ride on collection timing

        schedule.spy_return.result(timeout=5)
        assert (lock.is_locked, (tmp_path / "a").exists()) == (False, False)
    finally:
        loop.call_soon_threadsafe(loop.stop)
        runner.join(timeout=5)
        loop.close()


@pytest.mark.asyncio
async def test_async_del_inside_a_running_loop_schedules_release(tmp_path: Path, schedule: MockType) -> None:
    lock = AsyncSoftFileLock(str(tmp_path / "a"))
    await lock.acquire()

    lock.__del__()  # ruff:ignore[unnecessary-dunder-call]  # a finalizer test cannot ride on collection timing

    await schedule.spy_return
    assert (lock.is_locked, (tmp_path / "a").exists()) == (False, False)


@pytest.mark.parametrize(
    "stop",
    [
        pytest.param(lambda _schedule, executor: executor.shutdown(), id="release-fails"),
        pytest.param(lambda schedule, _executor: schedule.spy_return.cancel(), id="release-canceled"),
    ],
)
@pytest.mark.asyncio
async def test_async_del_inside_a_running_loop_falls_back_when_the_release_does_not_finish(
    tmp_path: Path, schedule: MockType, stop: Callable[[MockType, ThreadPoolExecutor], object]
) -> None:
    with ThreadPoolExecutor(max_workers=1) as executor:
        lock: Final[AsyncSoftFileLock] = AsyncSoftFileLock(str(tmp_path / "a"), executor=executor)
        await lock.acquire()

        lock.__del__()  # ruff:ignore[unnecessary-dunder-call]  # a finalizer test cannot ride on collection timing

        # A loop shutting down cancels the release before it runs; a shut-down executor fails it.
        stop(schedule, executor)
        await asyncio.wait([schedule.spy_return])
        await asyncio.sleep(0)  # the done callbacks run on the next loop iteration
    assert (lock.is_locked, (tmp_path / "a").exists()) == (False, False)


@NEEDS_PROMPT_FINALIZATION
@pytest.mark.asyncio
@pytest.mark.timeout(30)
async def test_async_del_never_waits_on_the_singleton_cache_lock(tmp_path: Path, schedule: MockType) -> None:
    # A constructor holds the cache lock while it takes the fork locks, inside which the collector can finalize a lock.
    class StallingLock(AsyncSoftFileLock):
        def __init__(self, lock_file: Path, *, is_singleton: bool) -> None:
            super().__init__(lock_file, is_singleton=is_singleton, run_in_executor=False)
            if next(constructions):
                inside.set()
                leave.wait()

    constructions: Final[count[int]] = count()
    inside: Final[threading.Event] = threading.Event()
    leave: Final[threading.Event] = threading.Event()
    lock = StallingLock(tmp_path / "a", is_singleton=True)
    await lock.acquire()
    holder: Final[threading.Thread] = threading.Thread(
        target=StallingLock, args=(tmp_path / "b",), kwargs={"is_singleton": True}
    )
    holder.start()
    inside.wait()
    try:
        del lock
    finally:
        leave.set()
        holder.join()
    await asyncio.wait([schedule.spy_return])

    assert not (tmp_path / "a").exists()


@pytest.mark.asyncio
async def test_async_thread_local_del_inside_a_running_loop_releases_another_threads_hold(tmp_path: Path) -> None:
    lock_path: Final[Path] = tmp_path / "a"
    lock: Final[AsyncSoftFileLock] = AsyncSoftFileLock(lock_path, thread_local=True, run_in_executor=False)
    holder: Final[threading.Thread] = threading.Thread(target=asyncio.run, args=(lock.acquire(),))
    holder.start()
    holder.join()

    lock.__del__()  # ruff:ignore[unnecessary-dunder-call]  # a finalizer test cannot ride on collection timing
    await asyncio.sleep(0)  # a release the finalizer scheduled on this loop would run here

    assert not lock_path.exists()


@NEEDS_PROMPT_FINALIZATION
@pytest.mark.asyncio
async def test_async_del_hands_the_singleton_slot_to_a_new_lock(tmp_path: Path, schedule: MockType) -> None:
    lock_path: Final[Path] = tmp_path / "a"
    # An executor worker keeps the call it ran until it picks up the next one, which would keep the lock alive.
    dying = AsyncSoftFileLock(lock_path, is_singleton=True, run_in_executor=False)
    await dying.acquire()

    del dying
    successor: Final[AsyncSoftFileLock] = AsyncSoftFileLock(lock_path, is_singleton=True, run_in_executor=False)
    await successor.acquire()
    await asyncio.wait([schedule.spy_return])

    assert (successor.is_locked, lock_path.exists()) == (True, True)
    await successor.release()


@NEEDS_PROMPT_FINALIZATION
@pytest.mark.parametrize("thread_local", [pytest.param(True, id="thread_local"), pytest.param(False, id="shared")])
def test_async_singleton_handed_out_while_finalizing_keeps_its_hold(tmp_path: Path, thread_local: bool) -> None:
    lock_path: Final[str] = str(tmp_path / "a")
    handed_out: Final[list[AsyncFileLock]] = []

    # The cache still finds a lock whose finalizer is starting, as another thread asking at that moment would.
    # Interpreters run a profile hook with tracing off, so coverage does not record its body.
    def take_from_cache(frame: FrameType, event: str, _arg: Unused) -> None:  # pragma: no cover
        if event == "call" and frame.f_code is BaseFileLock.__del__.__code__ and not handed_out:
            handed_out.append(
                AsyncFileLock(lock_path, is_singleton=True, thread_local=thread_local, run_in_executor=False)
            )
            asyncio.run(handed_out[0].acquire())

    sys.setprofile(take_from_cache)
    try:
        AsyncFileLock(lock_path, is_singleton=True, thread_local=thread_local, run_in_executor=False)
    finally:
        sys.setprofile(None)

    with pytest.raises(Timeout):
        FileLock(lock_path, blocking=False).acquire()
    asyncio.run(handed_out[0].release())


@pytest_asyncio.fixture(loop_scope="function")
async def schedule(mocker: MockerFixture) -> MockType:  # ruff:ignore[unused-async]  # runs on the test's loop
    return mocker.spy(asyncio.get_running_loop(), "create_task")


@NEEDS_PROMPT_FINALIZATION
@pytest.mark.parametrize("thread_local", [pytest.param(False, id="shared"), pytest.param(True, id="thread-local")])
def test_async_lock_on_a_daemon_loop_thread_held_at_interpreter_exit_releases_its_marker(
    tmp_path: Path, thread_local: bool
) -> None:
    script: Final[str] = (
        "import asyncio, sys, threading\n"
        "from filelock import AsyncSoftFileLock\n"
        "loop = asyncio.new_event_loop()\n"
        "threading.Thread(target=loop.run_forever, daemon=True).start()\n"
        f"lock = AsyncSoftFileLock(sys.argv[1], loop=loop, thread_local={thread_local}, "
        f"run_in_executor={not thread_local})\n"
        "asyncio.run_coroutine_threadsafe(lock.acquire(), loop).result()\n"
    )

    subprocess.run([sys.executable, "-c", script, str(tmp_path / "a")], check=True, timeout=10)

    assert not (tmp_path / "a").exists()
