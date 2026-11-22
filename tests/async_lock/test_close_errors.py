from __future__ import annotations

import os
import threading
from contextlib import contextmanager
from errno import EINTR, EIO
from typing import TYPE_CHECKING

import pytest

from filelock import (
    AsyncFileLock,
    AsyncSoftFileLock,
    BaseAsyncFileLock,
    CloseErrorPolicy,
)
from tests.capability_marks import (
    NEEDS_FCNTL,
)

if TYPE_CHECKING:
    from collections.abc import Generator
    from pathlib import Path

    from pytest_mock import MockerFixture

    from tests.async_lock.context import Enter

from tests.async_lock.context import ENTER


@pytest.mark.parametrize("policy", ["default", "raise"])
@pytest.mark.asyncio
async def test_soft_close_error_policy_cleans_marker(
    tmp_path: Path, mocker: MockerFixture, policy: CloseErrorPolicy
) -> None:
    lock_path = tmp_path / "a"
    lock = AsyncSoftFileLock(lock_path, close_error_policy=policy, run_in_executor=False)
    await lock.acquire()
    with _close_after_commit(mocker, lock) as (close_error, attempts):
        with pytest.raises(OSError, match="close failed") as info:
            await lock.release()
        assert info.value is close_error
    assert (len(attempts), lock.is_locked, lock.lock_counter, lock_path.exists()) == (1, False, 0, False)


@contextmanager
def _close_after_commit(mocker: MockerFixture, lock: AsyncSoftFileLock) -> Generator[tuple[OSError, list[int]]]:
    real_close = os.close
    close_error = OSError(EINTR, "close failed")
    attempts: list[int] = []
    # A finalizer can close an unrelated descriptor while os.close is patched.
    released: list[int | None] = []
    real_mark_released = lock._mark_descriptor_released

    def mark_released() -> None:
        released.append(lock._context.lock_file_fd)
        real_mark_released()

    def close(fd: int) -> None:
        real_close(fd)
        if fd not in released:
            return
        attempts.append(fd)
        raise close_error

    mocker.patch.object(lock, "_mark_descriptor_released", side_effect=mark_released, autospec=True)
    close_mock = mocker.patch.object(os, "close", side_effect=close, autospec=True)
    try:
        yield close_error, attempts
    finally:
        mocker.stop(close_mock)


@pytest.mark.asyncio
async def test_soft_close_error_suppress_policy_cleans_marker(tmp_path: Path, mocker: MockerFixture) -> None:
    lock_path = tmp_path / "a"
    lock = AsyncSoftFileLock(lock_path, close_error_policy="suppress", run_in_executor=False)
    await lock.acquire()
    with _close_after_commit(mocker, lock) as (_, attempts):
        await lock.release()
    assert (len(attempts), lock.is_locked, lock.lock_counter, lock_path.exists()) == (1, False, 0, False)


@pytest.mark.asyncio
async def test_soft_close_error_suppression_releases_nested_acquisitions_once(
    tmp_path: Path, mocker: MockerFixture
) -> None:
    lock_path = tmp_path / "a"
    lock = AsyncSoftFileLock(lock_path, close_error_policy="suppress", run_in_executor=False)
    await lock.acquire()
    await lock.acquire()
    with _close_after_commit(mocker, lock) as (_, attempts):
        await lock.release()
        await lock.release()
    assert (len(attempts), lock.is_locked, lock.lock_counter, lock_path.exists()) == (1, False, 0, False)


@pytest.mark.asyncio
async def test_soft_close_error_suppression_forced_release_releases_once(tmp_path: Path, mocker: MockerFixture) -> None:
    lock_path = tmp_path / "a"
    lock = AsyncSoftFileLock(lock_path, close_error_policy="suppress", run_in_executor=False)
    await lock.acquire()
    await lock.acquire()
    with _close_after_commit(mocker, lock) as (_, attempts):
        await lock.release(force=True)
    assert (len(attempts), lock.is_locked, lock.lock_counter, lock_path.exists()) == (1, False, 0, False)


@pytest.mark.parametrize("enter", ENTER)
@pytest.mark.asyncio
async def test_soft_context_surfaces_close_error_after_cleanup(
    tmp_path: Path,
    mocker: MockerFixture,
    *,
    enter: Enter,
) -> None:
    lock_path = tmp_path / "a"
    lock = AsyncSoftFileLock(lock_path, run_in_executor=False)
    with (
        _close_after_commit(mocker, lock) as (close_error, attempts),
        pytest.raises(OSError, match="close failed") as info,
    ):
        async with enter(lock):
            pass
    assert (info.value, len(attempts), lock.is_locked, lock_path.exists()) == (close_error, 1, False, False)


@pytest.mark.asyncio
async def test_soft_second_release_does_not_close_reused_descriptor(
    tmp_path: Path,
    mocker: MockerFixture,
) -> None:
    lock = AsyncSoftFileLock(tmp_path / "a", run_in_executor=False)
    await lock.acquire()
    with (
        _close_after_commit(mocker, lock) as (close_error, attempts),
        pytest.raises(OSError, match="close failed") as info,
    ):
        await lock.release()
    assert info.value is close_error
    reused_fd = os.open(tmp_path / "reused", os.O_CREAT | os.O_WRONLY)
    assert reused_fd == attempts[0]
    try:
        await lock.release()
        os.fstat(reused_fd)
    finally:
        os.close(reused_fd)


@pytest.mark.asyncio
async def test_close_after_commit_ignores_other_descriptors(tmp_path: Path, mocker: MockerFixture) -> None:
    # The patch binds os.close process-wide, so a descriptor that is not the lock's, whether closed on another thread
    # or by a finalizer the collector happens to run on this one, must pass through cleanly and stay out of the
    # recorded attempts.
    lock = AsyncSoftFileLock(tmp_path / "a", run_in_executor=False)
    await lock.acquire()
    closed: list[int] = []

    def close_a_pipe() -> None:
        read_fd, write_fd = os.pipe()
        os.close(read_fd)
        os.close(write_fd)
        closed.extend((read_fd, write_fd))

    with _close_after_commit(mocker, lock) as (_close_error, attempts):
        close_a_pipe()
        worker = threading.Thread(target=close_a_pipe)
        worker.start()
        worker.join()
        with pytest.raises(OSError, match="close failed"):
            await lock.release()

    assert (len(closed), len(attempts), attempts[0] in closed) == (4, 1, False)


@NEEDS_FCNTL
@pytest.mark.asyncio
async def test_close_error_raise_propagates(tmp_path: Path, mocker: MockerFixture) -> None:  # pragma: needs fcntl
    lock = AsyncFileLock(str(tmp_path / "a"), close_error_policy="raise")
    await lock.acquire()
    _fail_close_of(mocker, lock, OSError(EIO, "close failed"))
    with pytest.raises(OSError, match="close failed"):
        await lock.release()
    assert not lock.is_locked


def _fail_close_of(mocker: MockerFixture, lock: BaseAsyncFileLock, error: OSError) -> None:  # pragma: needs fcntl
    # Fail os.close only for this lock's descriptor, so an unrelated lock's __del__ close during the test is untouched.
    fd = lock._context.lock_file_fd
    real_close = os.close

    def close(target: int) -> None:
        if target == fd:
            raise error
        real_close(target)  # pragma: no cover  # only an unrelated __del__ close during the mock window reaches here

    mocker.patch("filelock._core._lock.os.close", side_effect=close, autospec=True)


@NEEDS_FCNTL
@pytest.mark.asyncio  # pragma: needs fcntl
async def test_close_error_default_suppressed_on_unix(tmp_path: Path, mocker: MockerFixture) -> None:
    lock = AsyncFileLock(str(tmp_path / "a"))  # default policy
    await lock.acquire()
    _fail_close_of(mocker, lock, OSError(EIO, "close failed"))
    await lock.release()
    assert not lock.is_locked
