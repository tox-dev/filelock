from __future__ import annotations

import asyncio
import threading
from typing import TYPE_CHECKING, Literal

import pytest

from filelock import Timeout
from filelock._soft_rw import AsyncSoftReadWriteLock, SoftReadWriteLock
from tests.capability_marks import XFAIL_WITHOUT_COROUTINE_CANCELLATION

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from pytest_mock import MockerFixture


def _make(tmp_path: Path) -> AsyncSoftReadWriteLock:
    return AsyncSoftReadWriteLock(
        str(tmp_path / "foo.lock"),
        is_singleton=False,
        heartbeat_interval=0.1,
        stale_threshold=0.5,
        poll_interval=0.02,
    )


class _Gate:
    """Pauses one call inside the executor thread so a cancellation lands after the backend work started."""

    def __init__(self, mocker: MockerFixture, name: str, *, fail_with: BaseException | None = None) -> None:
        self.started = asyncio.Event()
        #: Set once the gated backend call returns, so a test can tell "the executor finished" from "the caller
        #: stopped waiting for it".
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


def _markers(tmp_path: Path) -> list[str]:
    readers = tmp_path / "foo.lock.readers"
    reader_names = sorted(entry.name for entry in readers.iterdir()) if readers.is_dir() else []
    return (["<write>"] if (tmp_path / "foo.lock.write").exists() else []) + reader_names


@pytest.mark.parametrize("mode", [pytest.param("read", id="read"), pytest.param("write", id="write")])
@pytest.mark.asyncio
@XFAIL_WITHOUT_COROUTINE_CANCELLATION
async def test_acquire_cancellation_hands_the_claim_back(
    tmp_path: Path, mocker: MockerFixture, mode: Literal["read", "write"]
) -> None:
    gate = _Gate(mocker, f"acquire_{mode}")
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
    assert _markers(tmp_path) == []
    contender = SoftReadWriteLock(str(tmp_path / "foo.lock"), is_singleton=False, poll_interval=0.02)
    try:
        with contender.write_lock(timeout=5):
            pass
    finally:
        contender.close()
    await lock.close()


@pytest.mark.asyncio
@XFAIL_WITHOUT_COROUTINE_CANCELLATION
async def test_acquire_cancellation_surfaces_a_failed_acquire(tmp_path: Path, mocker: MockerFixture) -> None:
    acquire_error = Timeout(str(tmp_path / "foo.lock"))
    gate = _Gate(mocker, "acquire_write", fail_with=acquire_error)
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
    assert _markers(tmp_path) == []
    await lock.close()


@pytest.mark.asyncio
@XFAIL_WITHOUT_COROUTINE_CANCELLATION
async def test_acquire_cancellation_surfaces_a_failed_release(tmp_path: Path, mocker: MockerFixture) -> None:
    gate = _Gate(mocker, "acquire_write")
    release_error = RuntimeError("release failed")
    mocker.patch.object(SoftReadWriteLock, "release", side_effect=release_error)
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
async def test_release_cancellation_drains_the_release(tmp_path: Path, mocker: MockerFixture) -> None:
    lock = _make(tmp_path)
    await lock.acquire_write(timeout=5)
    gate = _Gate(mocker, "release")
    task = asyncio.create_task(lock.release())
    await gate.started.wait()
    task.cancel("cancel release")
    # Resume only after the cancellation has had a chance to propagate, so the cancellation reaching the caller
    # before the release finished is a failure rather than a coin flip.
    asyncio.get_running_loop().call_later(0.05, gate.resume)
    with pytest.raises(asyncio.CancelledError):
        await task

    assert gate.finished.is_set(), "the cancellation surfaced while the release was still running"
    assert lock._lock._hold is None
    assert _markers(tmp_path) == []
    await lock.close()


@pytest.mark.asyncio
@XFAIL_WITHOUT_COROUTINE_CANCELLATION
async def test_release_cancellation_surfaces_a_failed_release(tmp_path: Path, mocker: MockerFixture) -> None:
    lock = _make(tmp_path)
    await lock.acquire_write(timeout=5)
    release_error = RuntimeError("release failed")
    gate = _Gate(mocker, "release", fail_with=release_error)
    task = asyncio.create_task(lock.release())
    await gate.started.wait()
    task.cancel("cancel release")
    gate.resume()
    with pytest.raises(RuntimeError, match="release failed") as info:
        await task

    assert info.value is release_error
    assert isinstance(release_error.__context__, asyncio.CancelledError)
    mocker.stopall()
    await lock.close()
