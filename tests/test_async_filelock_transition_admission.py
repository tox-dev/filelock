from __future__ import annotations

import asyncio
import sys
import threading
from typing import TYPE_CHECKING, Final

import pytest
from async_filelock_cancellation_helpers import (
    assert_cancellation_message,
    assert_file_lock_state,
    start_file_lock_holder,
)

from filelock import AsyncFileLock, Timeout

if TYPE_CHECKING:
    from pathlib import Path
    from typing import Literal

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


@_UNIX_FLOCK_ONLY
@pytest.mark.asyncio
async def test_queued_acquire_proceeds_after_prior_waiter_cancels(tmp_path: Path) -> None:
    hook_started = asyncio.Event()
    finish_hook = threading.Event()
    loop = asyncio.get_running_loop()

    def block_hook(_fd: int) -> None:
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
    try:
        with pytest.raises(asyncio.CancelledError) as info:
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


def _start_signaled_acquire(lock: AsyncFileLock) -> tuple[asyncio.Task[None], asyncio.Event]:
    started = asyncio.Event()

    async def acquire() -> None:
        started.set()
        await lock.acquire()

    return asyncio.create_task(acquire()), started
