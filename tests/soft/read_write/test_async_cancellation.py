from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Final, Literal

import pytest

from filelock import AsyncSoftReadWriteLock, SoftReadWriteLock, Timeout
from tests.capability_marks import XFAIL_WITHOUT_COROUTINE_CANCELLATION

from .async_helpers import BackendGate, make_async_lock, members

if TYPE_CHECKING:
    from pathlib import Path

    from pytest_mock import MockerFixture


pytestmark: Final[list[pytest.MarkDecorator]] = [
    pytest.mark.requires_hard_links,
    pytest.mark.usefixtures("clear_singletons"),
]


@pytest.mark.parametrize("mode", [pytest.param("read", id="read"), pytest.param("write", id="write")])
@pytest.mark.asyncio
@XFAIL_WITHOUT_COROUTINE_CANCELLATION
async def test_async_acquire_cancellation_hands_the_claim_back(
    tmp_path: Path, mocker: MockerFixture, mode: Literal["read", "write"]
) -> None:
    gate = BackendGate(mocker, "_acquire_level_of", SoftReadWriteLock._acquire_level_of)
    lock = make_async_lock(tmp_path)
    task = asyncio.create_task(getattr(lock, f"acquire_{mode}")(timeout=5))
    await gate.started.wait()
    task.cancel("cancel acquire")
    gate.resume()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert gate.finished.wait(timeout=5), "the executor never finished the acquire it was already running"

    assert lock._lock._hold is None
    assert members(tmp_path) == []
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
    # An I/O error distinguishes backend failure from ordinary acquisition timeout.
    acquire_error = OSError("acquire failed")
    gate = BackendGate(mocker, "_acquire_level_of", SoftReadWriteLock._acquire_level_of, fail_with=acquire_error)
    lock = make_async_lock(tmp_path)
    task = asyncio.create_task(lock.acquire_write(timeout=5))
    await gate.started.wait()
    task.cancel("cancel acquire")
    gate.resume()
    with pytest.raises(OSError, match="acquire failed") as info:
        await task

    assert info.value is acquire_error
    assert isinstance(acquire_error.__context__, asyncio.CancelledError)
    assert lock._lock._hold is None
    assert members(tmp_path) == []
    await lock.close()


@pytest.mark.asyncio
@XFAIL_WITHOUT_COROUTINE_CANCELLATION
async def test_async_acquire_cancellation_surfaces_a_failed_release(tmp_path: Path, mocker: MockerFixture) -> None:
    gate = BackendGate(mocker, "_acquire_level_of", SoftReadWriteLock._acquire_level_of)
    release_error = RuntimeError("release failed")
    mocker.patch.object(SoftReadWriteLock, "_release_level_of", side_effect=release_error, autospec=True)
    lock = make_async_lock(tmp_path)
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
    lock = make_async_lock(tmp_path)
    gate = BackendGate(mocker, "_release_level_of", SoftReadWriteLock._release_level_of)
    task = asyncio.create_task(_acquire_then_release(lock, "write"))
    await gate.started.wait()
    task.cancel("cancel release")
    # Give cancellation a turn before allowing the release to finish.
    asyncio.get_running_loop().call_later(0.05, gate.resume)
    with pytest.raises(asyncio.CancelledError):
        await task

    assert gate.finished.is_set(), "the cancellation surfaced while the release was still running"
    assert lock._lock._hold is None
    assert members(tmp_path) == []
    await lock.close()


async def _acquire_then_release(lock: AsyncSoftReadWriteLock, mode: Literal["read", "write"]) -> None:
    # The task under cancellation must own the hold.
    await getattr(lock, f"acquire_{mode}")(timeout=5)
    await lock.release()


@pytest.mark.parametrize("mode", [pytest.param("read", id="read"), pytest.param("write", id="write")])
@pytest.mark.asyncio
@XFAIL_WITHOUT_COROUTINE_CANCELLATION
async def test_async_release_cancellation_hands_the_lock_to_the_next_task(
    tmp_path: Path, mocker: MockerFixture, mode: Literal["read", "write"]
) -> None:
    lock = make_async_lock(tmp_path)
    gate = BackendGate(mocker, "_release_level_of", SoftReadWriteLock._release_level_of)
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


@pytest.mark.asyncio
@XFAIL_WITHOUT_COROUTINE_CANCELLATION
async def test_async_close_cancellation_drains_the_close(tmp_path: Path, mocker: MockerFixture) -> None:
    lock = make_async_lock(tmp_path)
    await lock.acquire_write(timeout=5)
    gate = BackendGate(mocker, "close", SoftReadWriteLock.close)
    task = asyncio.create_task(lock.close())
    await gate.started.wait()
    task.cancel("cancel close")
    gate.resume()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert gate.finished.is_set()
    assert members(tmp_path) == []


@pytest.mark.asyncio
@XFAIL_WITHOUT_COROUTINE_CANCELLATION
async def test_async_close_cancellation_surfaces_a_failed_close(tmp_path: Path, mocker: MockerFixture) -> None:
    lock = make_async_lock(tmp_path)
    await lock.acquire_write(timeout=5)
    close_error = RuntimeError("close failed")
    gate = BackendGate(mocker, "close", SoftReadWriteLock.close, fail_with=close_error)
    task = asyncio.create_task(lock.close())
    await gate.started.wait()
    task.cancel("cancel close")
    gate.resume()
    with pytest.raises(RuntimeError) as info:
        await task

    assert gate.finished.is_set()
    assert info.value is close_error
    assert isinstance(close_error.__context__, asyncio.CancelledError)
    mocker.stopall()
    await lock.close()
    assert members(tmp_path) == []


@pytest.mark.asyncio
@XFAIL_WITHOUT_COROUTINE_CANCELLATION
async def test_async_release_cancellation_surfaces_a_failed_release(tmp_path: Path, mocker: MockerFixture) -> None:
    lock = make_async_lock(tmp_path)
    release_error = RuntimeError("release failed")
    gate = BackendGate(mocker, "_release_level_of", SoftReadWriteLock._release_level_of, fail_with=release_error)
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
