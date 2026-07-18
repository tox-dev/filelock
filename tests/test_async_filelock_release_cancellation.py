from __future__ import annotations

import asyncio
import logging
import sys
import threading
from errno import EIO
from typing import TYPE_CHECKING, Final

import pytest
from async_filelock_cancellation_helpers import (
    assert_cancellation_message,
    assert_file_lock_state,
    get_fcntl,
    start_file_lock_holder,
)

from filelock import AsyncFileLock, BaseAsyncFileLock, ContextErrorPolicy

if sys.version_info >= (3, 11):  # pragma: no cover (py311+)
    from builtins import BaseExceptionGroup, ExceptionGroup
else:  # pragma: no cover (<py311)
    from exceptiongroup import BaseExceptionGroup, ExceptionGroup

if TYPE_CHECKING:
    from pathlib import Path

    from pytest_mock import MockerFixture

_UNIX_FLOCK_ONLY: Final[pytest.MarkDecorator] = pytest.mark.skipif(
    sys.platform == "win32", reason="native flock semantics are Unix-only"
)


@_UNIX_FLOCK_ONLY
@pytest.mark.asyncio  # pragma: win32 no cover
async def test_release_completes_despite_cancellation(tmp_path: Path, mocker: MockerFixture) -> None:
    lock = AsyncFileLock(str(tmp_path / "a"))
    await lock.acquire()
    release_started = asyncio.Event()
    finish_release = threading.Event()
    loop = asyncio.get_running_loop()

    def block_unlock(_fd: int, _operation: int) -> None:  # pragma: win32 no cover
        loop.call_soon_threadsafe(release_started.set)
        assert finish_release.wait(timeout=5)

    mocker.patch("filelock._unix.fcntl.flock", side_effect=block_unlock)
    task = asyncio.create_task(lock.release())
    await release_started.wait()
    task.cancel("cancel release")
    finish_release.set()
    with pytest.raises(asyncio.CancelledError):  # pragma: win32 no cover
        await task
    assert (lock.is_locked, lock.lock_counter) == (False, 0)
    assert_file_lock_state(str(tmp_path / "a"), available=True)


@_UNIX_FLOCK_ONLY
@pytest.mark.asyncio  # pragma: win32 no cover
async def test_acquire_proceeds_after_queued_release_is_canceled(tmp_path: Path, mocker: MockerFixture) -> None:
    lock = AsyncFileLock(tmp_path / "a")
    await lock.acquire()
    release_started = asyncio.Event()
    finish_release = threading.Event()
    loop = asyncio.get_running_loop()
    fcntl = get_fcntl()
    real_flock = fcntl.flock
    blocked = False

    def block_first_unlock(fd: int, operation: int) -> None:  # pragma: win32 no cover
        nonlocal blocked
        if operation & fcntl.LOCK_UN and not blocked:  # pragma: win32 no cover
            blocked = True
            loop.call_soon_threadsafe(release_started.set)
            assert finish_release.wait(timeout=5)
        real_flock(fd, operation)

    mocker.patch("filelock._unix.fcntl.flock", side_effect=block_first_unlock)
    first_release = asyncio.create_task(lock.release())
    await release_started.wait()
    second_release = asyncio.create_task(lock.release())
    await asyncio.sleep(0)
    second_release.cancel("abandon queued release")
    try:  # pragma: win32 no cover
        with pytest.raises(asyncio.CancelledError) as info:  # pragma: win32 no cover
            await second_release
        assert_cancellation_message(info.value, "abandon queued release")
        acquire = asyncio.create_task(lock.acquire())
    finally:
        finish_release.set()

    await first_release
    await acquire
    assert (lock.is_locked, lock.lock_counter) == (True, 1)
    await lock.release()
    assert_file_lock_state(str(tmp_path / "a"), available=True)


@_UNIX_FLOCK_ONLY
@pytest.mark.asyncio
async def test_release_waits_for_provisional_acquire(tmp_path: Path) -> None:  # pragma: win32 no cover
    hook_started = asyncio.Event()
    finish_hook = threading.Event()
    loop = asyncio.get_running_loop()

    def block_hook(_fd: int) -> None:  # pragma: win32 no cover
        loop.call_soon_threadsafe(hook_started.set)
        assert finish_hook.wait(timeout=5)

    lock = AsyncFileLock(tmp_path / "a", on_acquired=block_hook)
    acquire_task = asyncio.create_task(lock.acquire())
    await hook_started.wait()
    release_started = asyncio.Event()

    async def release() -> None:  # pragma: win32 no cover
        release_started.set()
        await lock.release()

    release_task = asyncio.create_task(release())
    await release_started.wait()
    finish_hook.set()
    await acquire_task
    await release_task

    assert (lock.is_locked, lock.lock_counter) == (False, 0)
    assert_file_lock_state(str(tmp_path / "a"), available=True)


@pytest.mark.asyncio
async def test_release_returns_while_acquire_waits_for_external_holder(tmp_path: Path) -> None:
    holder, holder_started, finish_holder = start_file_lock_holder(str(tmp_path / "a"))
    assert await asyncio.to_thread(holder_started.wait, 5)
    first_polled = asyncio.Event()

    def observe_first_poll() -> bool:
        first_polled.set()
        return False

    lock = AsyncFileLock(tmp_path / "a")
    acquire_task = asyncio.create_task(lock.acquire(cancel_check=observe_first_poll, poll_interval=0.001))
    try:
        await first_polled.wait()
        await lock.release()
        assert (acquire_task.done(), lock.is_locked) == (False, False)
    finally:
        finish_holder.set()
        await asyncio.to_thread(holder.join, 5)

    assert not holder.is_alive()
    await acquire_task
    await lock.release()
    assert (lock.is_locked, lock.lock_counter) == (False, 0)
    assert_file_lock_state(str(tmp_path / "a"), available=True)


@_UNIX_FLOCK_ONLY
@pytest.mark.parametrize("policy", [pytest.param("chain", id="chain"), pytest.param("group", id="group")])
@pytest.mark.asyncio
async def test_release_cancellation_surfaces_backend_error(  # pragma: win32 no cover
    tmp_path: Path, mocker: MockerFixture, caplog: pytest.LogCaptureFixture, policy: ContextErrorPolicy
) -> None:
    lock = AsyncFileLock(tmp_path / "a", context_error_policy=policy)
    await lock.acquire()
    release_started = asyncio.Event()
    finish_release = threading.Event()
    loop = asyncio.get_running_loop()
    release_error = OSError(EIO, "release failed")
    release_count = 0

    def fail_first_unlock(_fd: int, _operation: int) -> None:  # pragma: win32 no cover
        nonlocal release_count
        release_count += 1
        if release_count == 1:  # pragma: win32 no cover
            loop.call_soon_threadsafe(release_started.set)
            assert finish_release.wait(timeout=5)
            raise release_error

    mocker.patch("filelock._unix.fcntl.flock", side_effect=fail_first_unlock)
    task = asyncio.create_task(lock.release())
    await release_started.wait()
    task.cancel("cancel release")
    finish_release.set()

    if policy == "chain":  # pragma: win32 no cover
        with pytest.raises(OSError, match="release failed") as info:  # pragma: win32 no cover
            await task
        cancellation = info.value.__context__
        assert info.value is release_error
    else:  # pragma: win32 no cover
        with pytest.raises(BaseExceptionGroup) as info:  # pragma: win32 no cover
            await task
        cancellation, grouped_release_error = info.value.exceptions
        assert grouped_release_error is release_error
        assert (info.value.__cause__, info.value.__suppress_context__) == (None, True)
    assert isinstance(cancellation, asyncio.CancelledError)
    assert (cancellation.args, release_error.__cause__, release_error.__context__) == (
        ("cancel release",),
        None,
        cancellation if policy == "chain" else None,
    )
    assert (cancellation.__traceback__ is not None, release_error.__traceback__ is not None) == (True, True)
    assert not any(record.name == "asyncio" and record.levelno >= logging.ERROR for record in caplog.records)
    assert (lock.is_locked, lock.lock_counter) == (True, 1)
    assert_file_lock_state(str(tmp_path / "a"), available=False)
    await lock.release()


@pytest.mark.asyncio
async def test_context_chain_does_not_duplicate_body_already_in_release_group(tmp_path: Path) -> None:
    body_error = ValueError("body failed")
    release_group = ExceptionGroup("release failed", (body_error,))
    fail_release = True

    class GroupReleaseLock(BaseAsyncFileLock):
        def _acquire(self) -> None:
            self._context.lock_file_fd = 1

        def _release(self) -> None:
            nonlocal fail_release
            if fail_release:
                fail_release = False
                raise release_group
            self._context.lock_file_fd = None

    lock = GroupReleaseLock(tmp_path / "a", run_in_executor=False, context_error_policy="chain")
    with pytest.raises(ExceptionGroup) as info:
        async with lock:
            raise body_error
    assert (info.value, release_group.__context__, release_group.exceptions) == (
        release_group,
        body_error,
        (body_error,),
    )
    await lock.release()
