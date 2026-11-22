from __future__ import annotations

import logging
import signal
from errno import EIO
from typing import TYPE_CHECKING

import pytest

from filelock import (
    AsyncFileLock,
    AsyncSoftFileLock,
    BaseAsyncFileLock,
)
from tests.capability_marks import (
    NEEDS_FCNTL,
    NEEDS_PROMPT_SIGNAL_DELIVERY,
)

if TYPE_CHECKING:
    from pathlib import Path

    from pytest_mock import MockerFixture


@pytest.mark.parametrize("lock_type", [AsyncFileLock, AsyncSoftFileLock])
@pytest.mark.asyncio
async def test_non_executor_release_interrupted_before_the_backend_completes(
    lock_type: type[BaseAsyncFileLock], tmp_path: Path, mocker: MockerFixture
) -> None:
    lock = lock_type(str(tmp_path / "a"), run_in_executor=False)
    await lock.acquire()
    mocker.patch(
        "filelock._async_lock._release._LOGGER.debug",
        autospec=True,
        side_effect=[KeyboardInterrupt, None, None],
    )

    with pytest.raises(KeyboardInterrupt):
        await lock.release()
    mocker.stopall()

    assert not lock.is_locked
    async with lock_type(str(tmp_path / "a"), blocking=False) as peer:
        assert peer.is_locked


@NEEDS_PROMPT_SIGNAL_DELIVERY  # pragma: needs prompt-signal-delivery
@pytest.mark.asyncio
async def test_non_executor_release_interrupted_by_a_signal_before_the_backend_completes(
    tmp_path: Path, mocker: MockerFixture
) -> None:
    lock = AsyncFileLock(str(tmp_path / "a"), run_in_executor=False)
    await lock.acquire()
    pending = [signal.SIGINT]

    def signal_once(*_args: object) -> None:
        signals = pending.copy()
        pending.clear()
        list(map(signal.raise_signal, signals))

    mocker.patch("filelock._async_lock._release._LOGGER.debug", autospec=True, side_effect=signal_once)
    previous = signal.signal(signal.SIGINT, signal.default_int_handler)
    try:
        with pytest.raises(KeyboardInterrupt):
            await lock.release()
    finally:
        signal.signal(signal.SIGINT, previous)
    mocker.stopall()

    assert not lock.is_locked
    async with AsyncFileLock(str(tmp_path / "a"), blocking=False) as peer:
        assert peer.is_locked


@pytest.mark.parametrize("lock_type", [AsyncSoftFileLock, AsyncFileLock])
@pytest.mark.asyncio
async def test_attempting_to_release(
    lock_type: type[BaseAsyncFileLock], tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    lock = lock_type(str(tmp_path / "a.lock"), run_in_executor=False)

    await lock.acquire(timeout=0.1)
    await lock.acquire(timeout=0.1)  # reentrant acquire
    await lock.release(force=True)

    assert any("Attempting to release lock" in m for m in caplog.messages)
    assert any("released" in m for m in caplog.messages)


@pytest.mark.parametrize("lock_type", [AsyncFileLock, AsyncSoftFileLock])
@pytest.mark.asyncio
async def test_release_early_exit_when_unlocked(lock_type: type[BaseAsyncFileLock], tmp_path: Path) -> None:
    lock = lock_type(str(tmp_path / "a.lock"), run_in_executor=False)
    assert not lock.is_locked
    await lock.release()
    assert not lock.is_locked


@pytest.mark.parametrize("lock_type", [AsyncFileLock, AsyncSoftFileLock])
@pytest.mark.asyncio
async def test_release_nonzero_counter_exit(
    lock_type: type[BaseAsyncFileLock], tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    lock = lock_type(str(tmp_path / "a.lock"), run_in_executor=False)
    await lock.acquire()
    await lock.acquire()
    await lock.release()
    assert lock.lock_counter == 1
    assert lock.is_locked
    assert not any("Attempting to release" in m for m in caplog.messages)
    await lock.release()


@NEEDS_FCNTL
@pytest.mark.asyncio  # pragma: needs fcntl
async def test_release_keeps_lock_held_when_unlock_fails(tmp_path: Path, mocker: MockerFixture) -> None:
    lock = AsyncFileLock(str(tmp_path / "a"))
    await lock.acquire()
    mocker.patch("filelock._native._unix.fcntl.flock", side_effect=[OSError(EIO, "unlock failed"), None], autospec=True)
    with pytest.raises(OSError, match="unlock failed"):
        await lock.release()
    assert lock.is_locked
    assert lock.lock_counter == 1
    await lock.release()
    assert not lock.is_locked


@pytest.mark.asyncio
async def test_async_zero_write_rolls_back_acquire(tmp_path: Path, mocker: MockerFixture) -> None:
    mocker.patch("filelock._filesystem._files.os.write", return_value=0, autospec=True)

    lock = AsyncSoftFileLock(str(tmp_path / "a"))
    with pytest.raises(OSError, match="0 bytes"):
        await lock.acquire()
    assert not lock.is_locked
