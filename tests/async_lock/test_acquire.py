from __future__ import annotations

import logging
import os
import threading
from typing import TYPE_CHECKING, Literal, cast

import pytest

from filelock import (
    AsyncFileLock,
    AsyncSoftFileLock,
    BaseAsyncFileLock,
    Timeout,
)
from tests.capability_marks import (
    NEEDS_STALE_RECLAIM,
)

if TYPE_CHECKING:
    from pathlib import Path

    from pytest_mock import MockerFixture


@pytest.mark.parametrize("lock_type", [AsyncFileLock, AsyncSoftFileLock])
@pytest.mark.parametrize("entry_point", ["constructor", "setter", "acquire"])
@pytest.mark.parametrize(
    ("bad_value", "error_type", "message"),
    [
        pytest.param(-1, ValueError, "finite and non-negative", id="negative-int"),
        pytest.param(-0.5, ValueError, "finite and non-negative", id="negative-float"),
        pytest.param(float("nan"), ValueError, "finite and non-negative", id="nan"),
        pytest.param(float("inf"), ValueError, "finite and non-negative", id="positive-infinity"),
        pytest.param(float("-inf"), ValueError, "finite and non-negative", id="negative-infinity"),
        pytest.param(threading.TIMEOUT_MAX * 2, ValueError, "TIMEOUT_MAX", id="above-timeout-max"),
        pytest.param(True, TypeError, "poll_interval must be", id="true"),
        pytest.param(False, TypeError, "poll_interval must be", id="false"),
        pytest.param("5", TypeError, "poll_interval must be", id="string"),
    ],
)
@pytest.mark.asyncio
async def test_poll_interval_rejects_invalid_value(
    lock_type: type[BaseAsyncFileLock],
    entry_point: Literal["constructor", "setter", "acquire"],
    bad_value: float | str,
    error_type: type[ValueError | TypeError],
    message: str,
    tmp_path: Path,
) -> None:
    lock = lock_type(str(tmp_path / "a"))
    with pytest.raises(error_type, match=message):
        await _set_poll_interval(lock_type, entry_point, lock, bad_value)


async def _set_poll_interval(
    lock_type: type[BaseAsyncFileLock],
    entry_point: Literal["constructor", "setter", "acquire"],
    lock: BaseAsyncFileLock,
    value: float | str,
) -> None:
    value = cast("float", value)
    if entry_point == "constructor":
        lock_type(lock.lock_file, poll_interval=value)
    elif entry_point == "setter":
        lock.poll_interval = value
    else:
        await lock.acquire(poll_interval=value, timeout=0)


@pytest.mark.parametrize("lock_type", [AsyncFileLock, AsyncSoftFileLock])
@pytest.mark.parametrize("blocking", [pytest.param("false", id="str"), pytest.param(0, id="int")])
@pytest.mark.asyncio
async def test_acquire_rejects_non_bool_blocking(
    lock_type: type[BaseAsyncFileLock], blocking: str | int, tmp_path: Path
) -> None:
    lock = lock_type(str(tmp_path / "a"))
    with pytest.raises(TypeError, match="blocking must be a bool"):
        await lock.acquire(blocking=blocking)  # ty: ignore[invalid-argument-type]  # the rejected type is the contract
    assert (lock.lock_counter, lock.is_locked) == (0, False)


@pytest.mark.parametrize("lock_type", [AsyncFileLock, AsyncSoftFileLock])
@pytest.mark.parametrize("interrupted", ["_raise_if_would_deadlock", "_pin_lock_path"])
@pytest.mark.asyncio
async def test_early_async_acquire_exception_leaves_no_counted_hold(
    lock_type: type[BaseAsyncFileLock], interrupted: str, tmp_path: Path, mocker: MockerFixture
) -> None:
    lock_path = str(tmp_path / "a.lock")
    lock = lock_type(lock_path)
    mocker.patch.object(lock, interrupted, side_effect=KeyboardInterrupt, autospec=True)

    with pytest.raises(KeyboardInterrupt):
        await lock.acquire()
    mocker.stopall()

    assert (lock.is_locked, lock.lock_counter) == (False, 0)
    async with lock:
        assert lock.lock_counter == 1
    assert (lock.is_locked, lock.lock_counter) == (False, 0)
    async with lock_type(lock_path, blocking=False, is_singleton=False) as peer:
        assert peer.is_locked


@pytest.mark.asyncio
@pytest.mark.parametrize("run_in_executor", [False, True])
async def test_failed_async_proxy_releases_provisional_hold(
    tmp_path: Path, mocker: MockerFixture, *, run_in_executor: bool
) -> None:
    lock = AsyncSoftFileLock(tmp_path / "proxy.lock", run_in_executor=run_in_executor)
    mocker.patch("filelock._async_lock._lock.AsyncAcquireReturnProxy", side_effect=MemoryError, autospec=True)

    with pytest.raises(MemoryError):
        await lock.acquire()
    mocker.stopall()

    assert (lock.is_locked, lock.lock_counter) == (False, 0)
    async with AsyncSoftFileLock(lock.lock_file, blocking=False, is_singleton=False) as peer:
        assert peer.is_locked


@pytest.mark.asyncio
async def test_reentrant_async_proxy_failure_keeps_one_level(tmp_path: Path, mocker: MockerFixture) -> None:
    lock = AsyncFileLock(str(tmp_path / "a.lock"))
    await lock.acquire()
    mocker.patch("filelock._async_lock._lock.AsyncAcquireReturnProxy", side_effect=KeyboardInterrupt, autospec=True)

    with pytest.raises(KeyboardInterrupt):
        await lock.acquire()
    mocker.stopall()

    assert (lock.is_locked, lock.lock_counter) == (True, 1)
    await lock.release()
    assert (lock.is_locked, lock.lock_counter) == (False, 0)


@pytest.mark.parametrize("lock_type", [AsyncFileLock, AsyncSoftFileLock])
@pytest.mark.asyncio
async def test_wait_message_logged(
    lock_type: type[BaseAsyncFileLock], tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    lock_path = tmp_path / "a"
    first_lock = lock_type(str(lock_path))
    second_lock = lock_type(str(lock_path), timeout=0.2)

    # Hold the lock so second_lock has to wait
    await first_lock.acquire()
    with pytest.raises(Timeout):
        await second_lock.acquire()
    assert any("waiting" in msg for msg in caplog.messages)


@pytest.mark.parametrize("lock_type", [AsyncSoftFileLock, AsyncFileLock])
@pytest.mark.asyncio
async def test_attempting_to_acquire_branch(
    lock_type: type[BaseAsyncFileLock], tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)

    lock = lock_type(str(tmp_path / "a"))
    await lock.acquire()
    assert any("Attempting to acquire lock" in m for m in caplog.messages)
    await lock.release()


@pytest.mark.asyncio
async def test_thread_local_run_in_executor(tmp_path: Path) -> None:  # ruff:ignore[unused-async]  # the asyncio marker requires a coroutine though the body awaits nothing
    with pytest.raises(ValueError, match="run_in_executor is not supported when thread_local is True"):
        AsyncSoftFileLock(str(tmp_path / "a"), thread_local=True, run_in_executor=True)


@pytest.mark.parametrize("lock_type", [AsyncSoftFileLock, AsyncFileLock])
@pytest.mark.asyncio
async def test_attempting_to_acquire(
    lock_type: type[BaseAsyncFileLock], tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    lock = lock_type(str(tmp_path / "a.lock"), run_in_executor=False)
    await lock.acquire(timeout=0.1)
    assert any("Attempting to acquire lock" in m for m in caplog.messages)
    await lock.release()


@pytest.mark.parametrize("lock_type", [AsyncFileLock, AsyncSoftFileLock])
@pytest.mark.asyncio
async def test_cancel_check_triggers(lock_type: type[BaseAsyncFileLock], tmp_path: Path) -> None:
    lock_path = tmp_path / "a"
    lock_1 = lock_type(str(lock_path))
    lock_2 = lock_type(str(lock_path))

    await lock_1.acquire()

    with pytest.raises(Timeout, match=r"The file lock '.*' could not be acquired."):
        await lock_2.acquire(timeout=1, cancel_check=lambda: True)
    assert not lock_2.is_locked
    await lock_1.release()


@pytest.mark.parametrize("lock_type", [AsyncFileLock, AsyncSoftFileLock])
@pytest.mark.asyncio
async def test_cancel_check_not_called_when_lock_available(lock_type: type[BaseAsyncFileLock], tmp_path: Path) -> None:
    lock = lock_type(str(tmp_path / "a"))

    called = False

    def should_not_be_called() -> bool:  # pragma: no cover  # a free lock never consults cancel_check
        nonlocal called
        called = True
        return True

    await lock.acquire(cancel_check=should_not_be_called)
    assert lock.is_locked
    assert not called
    await lock.release()


@pytest.mark.parametrize("lock_type", [AsyncFileLock, AsyncSoftFileLock])
@pytest.mark.asyncio
async def test_cancel_check_log_message(
    lock_type: type[BaseAsyncFileLock], tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    lock_path = tmp_path / "a"
    lock_1 = lock_type(str(lock_path))
    lock_2 = lock_type(str(lock_path))

    await lock_1.acquire()
    with pytest.raises(Timeout):
        await lock_2.acquire(timeout=1, cancel_check=lambda: True)
    assert any("Cancellation requested" in msg for msg in caplog.messages)
    await lock_1.release()


@pytest.mark.filterwarnings("ignore::filelock.SoftFileLockLifetimeWarning")
@pytest.mark.asyncio
@NEEDS_STALE_RECLAIM
async def test_executor_acquire_keeps_filesystem_calls_off_the_loop(tmp_path: Path, mocker: MockerFixture) -> None:
    lock_path = tmp_path / "a"
    lock_path.touch()
    os.utime(lock_path, (0, 0))
    lock = AsyncSoftFileLock(str(lock_path), lifetime=1)
    real_lstat = os.lstat
    lstat_threads: set[int] = set()

    def lstat(path: str, *, dir_fd: int | None = None) -> os.stat_result:
        if os.fspath(path) == lock.lock_file:
            lstat_threads.add(threading.get_ident())
        return real_lstat(path, dir_fd=dir_fd)

    mocker.patch("os.lstat", side_effect=lstat, autospec=True)
    await lock.acquire()
    mocker.stopall()
    await lock.release()

    # The patch is process-wide, so only the lock's own path counts; unrelated loop-thread lstats are not ours.
    assert lstat_threads
    assert threading.get_ident() not in lstat_threads
