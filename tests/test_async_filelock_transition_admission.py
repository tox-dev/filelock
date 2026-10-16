from __future__ import annotations

import asyncio
import sys
import threading
from typing import TYPE_CHECKING, Final

import pytest

from filelock import AsyncFileLock, AsyncSoftFileLock, BaseAsyncFileLock, Timeout
from filelock._async import _AsyncTransitionGate
from tests.async_filelock_cancellation_helpers import (
    assert_cancellation_message,
    assert_file_lock_state,
    start_file_lock_holder,
)
from tests.capability_marks import XFAIL_WITHOUT_COROUTINE_CANCELLATION

if TYPE_CHECKING:
    from concurrent.futures import Future as ConcurrentFuture
    from pathlib import Path
    from typing import Literal

    from pytest_mock import MockerFixture

_UNIX_FLOCK_ONLY: Final[pytest.MarkDecorator] = pytest.mark.skipif(
    sys.platform == "win32", reason="native flock semantics are Unix-only"
)


@pytest.mark.parametrize(
    "admission",
    [
        pytest.param("nonblocking", id="nonblocking"),
        pytest.param("deadline", id="finite-deadline"),
        pytest.param("cancel-check", id="cancel-check"),
    ],
)
@pytest.mark.asyncio
@XFAIL_WITHOUT_COROUTINE_CANCELLATION
async def test_queued_acquire_honors_own_admission_policy(
    tmp_path: Path, admission: Literal["nonblocking", "deadline", "cancel-check"]
) -> None:
    first_polled = asyncio.Event()

    def observe_first_poll() -> bool:
        first_polled.set()
        return False

    holder, holder_started, finish_holder = start_file_lock_holder(str(tmp_path / "a"))
    assert await asyncio.to_thread(holder_started.wait, 5)
    lock = AsyncFileLock(tmp_path / "a")
    first_task = asyncio.create_task(lock.acquire(cancel_check=observe_first_poll, poll_interval=0.001))
    try:
        await first_polled.wait()
        if admission == "nonblocking":
            with pytest.raises(Timeout):
                await lock.acquire(blocking=False)
        elif admission == "deadline":
            with pytest.raises(Timeout):
                await lock.acquire(timeout=0.01)
        else:
            cancel_second = asyncio.Event()
            second_started = asyncio.Event()

            async def acquire_second() -> None:
                second_started.set()
                await lock.acquire(cancel_check=cancel_second.is_set, poll_interval=0.001)

            second_task = asyncio.create_task(acquire_second())
            await second_started.wait()
            cancel_second.set()
            with pytest.raises(Timeout):
                await second_task
        assert not first_task.done()
    finally:
        finish_holder.set()
        await asyncio.to_thread(holder.join, 5)

    assert not holder.is_alive()
    await first_task
    await lock.release()
    assert (lock.is_locked, lock.lock_counter) == (False, 0)
    assert_file_lock_state(str(tmp_path / "a"), available=True)


@pytest.mark.parametrize(
    "lock_type", [pytest.param(AsyncFileLock, id="flock"), pytest.param(AsyncSoftFileLock, id="soft")]
)
@pytest.mark.asyncio
async def test_acquire_interrupted_while_joining_the_gate_leaves_the_lock_usable(
    tmp_path: Path, mocker: MockerFixture, lock_type: type[AsyncFileLock | AsyncSoftFileLock]
) -> None:
    real_join: Final = _AsyncTransitionGate._join
    interrupted = False

    def join_then_interrupt(instance: _AsyncTransitionGate, ticket: ConcurrentFuture[None]) -> None:
        nonlocal interrupted
        real_join(instance, ticket)
        if not interrupted:
            interrupted = True
            raise _Interrupt

    mocker.patch.object(_AsyncTransitionGate, "_join", autospec=True, side_effect=join_then_interrupt)
    lock = lock_type(tmp_path / "a")
    with pytest.raises(_Interrupt):
        await lock.acquire()

    await asyncio.wait_for(lock.acquire(), 5)

    assert lock.is_locked
    await lock.release()


@pytest.mark.asyncio
async def test_acquires_giving_up_behind_a_waiter_leave_the_lock_releasable(tmp_path: Path) -> None:
    first_polled = asyncio.Event()

    def observe_first_poll() -> bool:
        first_polled.set()
        return False

    holder, holder_started, finish_holder = start_file_lock_holder(str(tmp_path / "a"))
    assert await asyncio.to_thread(holder_started.wait, 5)
    lock = AsyncFileLock(tmp_path / "a")
    waiter = asyncio.create_task(lock.acquire(cancel_check=observe_first_poll, poll_interval=0.001))
    try:
        await first_polled.wait()
        # More callers than the recursion limit allows frames for, should they be settled one inside the other.
        for _ in range(sys.getrecursionlimit()):
            with pytest.raises(Timeout):
                await lock.acquire(blocking=False)
    finally:
        finish_holder.set()
        await asyncio.to_thread(holder.join, 5)
    await waiter

    await asyncio.wait_for(lock.release(), 5)

    assert (lock.is_locked, lock.lock_counter) == (False, 0)
    assert_file_lock_state(str(tmp_path / "a"), available=True)


@_UNIX_FLOCK_ONLY
@pytest.mark.asyncio
@XFAIL_WITHOUT_COROUTINE_CANCELLATION
async def test_queued_acquire_proceeds_after_prior_waiter_cancels(tmp_path: Path) -> None:  # pragma: win32 no cover
    hook_started = asyncio.Event()
    finish_hook = threading.Event()
    loop = asyncio.get_running_loop()

    def block_hook(_fd: int) -> None:  # pragma: win32 no cover
        loop.call_soon_threadsafe(hook_started.set)
        assert finish_hook.wait(timeout=5)

    lock = AsyncFileLock(tmp_path / "a", on_acquired=block_hook)
    first_task = asyncio.create_task(lock.acquire())
    await hook_started.wait()
    second_task, second_started = _start_signaled_acquire(lock)
    await second_started.wait()
    third_task, third_started = _start_signaled_acquire(lock)
    await third_started.wait()
    second_task.cancel("abandon queued acquire")
    try:  # pragma: win32 no cover
        with pytest.raises(asyncio.CancelledError) as info:  # pragma: win32 no cover
            await second_task
        assert_cancellation_message(info.value, "abandon queued acquire")
    finally:
        finish_hook.set()

    await first_task
    await third_task
    assert (lock.is_locked, lock.lock_counter) == (True, 2)
    await lock.release()
    await lock.release()
    assert (lock.is_locked, lock.lock_counter) == (False, 0)
    assert_file_lock_state(str(tmp_path / "a"), available=True)


def _start_signaled_acquire(lock: AsyncFileLock) -> tuple[asyncio.Task[None], asyncio.Event]:  # pragma: win32 no cover
    started = asyncio.Event()

    async def acquire() -> None:  # pragma: win32 no cover
        started.set()
        await lock.acquire()

    return asyncio.create_task(acquire()), started


@pytest.mark.parametrize(
    "lock_type", [pytest.param(AsyncFileLock, id="flock"), pytest.param(AsyncSoftFileLock, id="soft")]
)
@pytest.mark.asyncio
async def test_thread_local_lock_releases_while_another_thread_waits_for_it(
    tmp_path: Path, lock_type: type[BaseAsyncFileLock]
) -> None:
    lock = lock_type(tmp_path / "a", thread_local=True, run_in_executor=False)
    await lock.acquire()
    polling = threading.Event()

    def note_poll() -> bool:
        polling.set()
        return False

    async def acquire_and_release() -> None:
        await lock.acquire(timeout=5, cancel_check=note_poll, poll_interval=0.01)
        await lock.release()

    waiter = asyncio.create_task(asyncio.to_thread(asyncio.run, acquire_and_release()))
    assert await asyncio.to_thread(polling.wait, 5)

    await asyncio.wait_for(lock.release(), 5)

    await waiter
    assert not lock.is_locked


class _Interrupt(BaseException):
    """Stands in for an exception a signal handler raises, without pytest treating it as a KeyboardInterrupt."""
