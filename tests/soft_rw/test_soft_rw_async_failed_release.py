from __future__ import annotations

import asyncio
import errno
from typing import TYPE_CHECKING, Final

import pytest

from filelock import Timeout
from filelock._soft_rw import AsyncSoftReadWriteLock, SoftReadWriteLock
from tests.capability_marks import XFAIL_WITHOUT_COROUTINE_CANCELLATION

if TYPE_CHECKING:
    from pathlib import Path

    from pytest_mock import MockerFixture

pytestmark = pytest.mark.requires_hard_links


@pytest.fixture
def lock_file(tmp_path: Path) -> str:
    return str(tmp_path / "foo.lock")


@pytest.fixture
def failing_release(mocker: MockerFixture) -> None:
    # The sync lock drops its hold before the filesystem step that fails, as a full disk does.
    real_release = SoftReadWriteLock.release
    pending = True

    def release(lock: SoftReadWriteLock, *, force: bool = False) -> None:
        nonlocal pending
        real_release(lock, force=force)
        if pending:
            pending = False
            raise OSError(errno.ENOSPC, "No space left on device")

    mocker.patch.object(SoftReadWriteLock, "release", autospec=True, side_effect=release)


def _make(lock_file: str) -> AsyncSoftReadWriteLock:
    return AsyncSoftReadWriteLock(
        lock_file, is_singleton=False, heartbeat_interval=0.1, stale_threshold=0.5, poll_interval=0.02
    )


@pytest.mark.usefixtures("failing_release")
@pytest.mark.asyncio
async def test_async_soft_read_after_a_failed_release_waits_for_another_writer(lock_file: str) -> None:
    lock: Final = _make(lock_file)
    await lock.acquire_read(timeout=5)
    with pytest.raises(OSError, match="No space left"):
        await lock.release()
    writer: Final = SoftReadWriteLock(lock_file, is_singleton=False, heartbeat_interval=0.1, stale_threshold=0.5)
    await asyncio.to_thread(writer.acquire_write, 5)

    with pytest.raises(Timeout):
        await asyncio.create_task(lock.acquire_read(blocking=False))
    writer.close()
    await lock.close()


@pytest.mark.usefixtures("failing_release")
@pytest.mark.asyncio
async def test_async_soft_release_retried_after_the_hold_was_dropped_raises(lock_file: str) -> None:
    lock: Final = _make(lock_file)
    await lock.acquire_write(timeout=5)
    with pytest.raises(OSError, match="No space left"):
        await lock.release()

    with pytest.raises(RuntimeError, match="not held by this task"):
        await lock.release()
    await lock.close()


@pytest.mark.asyncio
@XFAIL_WITHOUT_COROUTINE_CANCELLATION
async def test_async_soft_close_cancellation_refuses_later_acquires(lock_file: str) -> None:
    lock: Final = _make(lock_file)
    await lock.acquire_read(timeout=5)
    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0):  # expires while close() waits for its executor call
            await lock.close()

    with pytest.raises(RuntimeError, match="has been closed"):
        await asyncio.create_task(lock.acquire_read(timeout=5))
