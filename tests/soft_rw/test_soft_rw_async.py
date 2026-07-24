from __future__ import annotations

import asyncio
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING, Literal

import pytest

from filelock import Timeout
from filelock._soft_rw import AsyncSoftReadWriteLock, SoftReadWriteLock
from tests.capability_marks import XFAIL_WITHOUT_COROUTINE_CANCELLATION

if TYPE_CHECKING:
    from collections.abc import Callable, Generator
    from concurrent.futures import Executor
    from pathlib import Path

    from pytest_mock import MockerFixture


@pytest.fixture(autouse=True)
def _clear_singletons() -> Generator[None]:
    SoftReadWriteLock._instances.clear()
    yield
    for lock in filter(None, (ref() for ref in list(SoftReadWriteLock._instances.valuerefs()))):
        lock.close()
    SoftReadWriteLock._instances.clear()


def _make(
    tmp_path: Path,
    name: str = "foo.lock",
    *,
    timeout: float = -1,
    blocking: bool = True,
    heartbeat_interval: float = 0.1,
    stale_threshold: float = 0.5,
    poll_interval: float = 0.02,
    loop: asyncio.AbstractEventLoop | None = None,
    executor: Executor | None = None,
) -> AsyncSoftReadWriteLock:
    return AsyncSoftReadWriteLock(
        str(tmp_path / name),
        timeout=timeout,
        blocking=blocking,
        is_singleton=False,
        heartbeat_interval=heartbeat_interval,
        stale_threshold=stale_threshold,
        poll_interval=poll_interval,
        loop=loop,
        executor=executor,
    )


class _Gate:
    """Pause one call inside the executor thread so a cancellation lands after the backend work started."""

    def __init__(self, mocker: MockerFixture, name: str, *, fail_with: BaseException | None = None) -> None:
        self.started = asyncio.Event()
        # Set once the gated call returns, letting a test distinguish the executor finishing from the caller giving up.
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


@pytest.mark.asyncio
async def test_async_write_lock_context_manager(tmp_path: Path) -> None:
    lock = _make(tmp_path)
    try:
        async with lock.write_lock(timeout=2):
            pass
    finally:
        await lock.close()


@pytest.mark.asyncio
async def test_async_read_lock_context_manager(tmp_path: Path) -> None:
    lock = _make(tmp_path)
    try:
        async with lock.read_lock(timeout=2):
            pass
    finally:
        await lock.close()


@pytest.mark.asyncio
async def test_async_exposes_configuration(tmp_path: Path) -> None:
    lock = _make(tmp_path, timeout=5, blocking=False)
    try:
        assert lock.lock_file.endswith("foo.lock")
        assert lock.timeout == 5
        assert lock.blocking is False
        assert lock.loop is None
        assert lock.executor is None
    finally:
        await lock.close()


@pytest.mark.asyncio
async def test_async_raw_acquire_release_round_trip(tmp_path: Path) -> None:
    lock = _make(tmp_path)
    try:
        proxy = await lock.acquire_write(timeout=2)
        async with proxy:
            pass
        proxy = await lock.acquire_read(timeout=2)
        async with proxy:
            pass
    finally:
        await lock.close()


@pytest.mark.asyncio
async def test_async_custom_loop_and_executor(tmp_path: Path) -> None:
    with ThreadPoolExecutor(max_workers=1) as executor:
        loop = asyncio.get_running_loop()
        lock = _make(tmp_path, loop=loop, executor=executor)
        try:
            assert lock.loop is loop
            assert lock.executor is executor
            async with lock.write_lock(timeout=2):
                pass
        finally:
            await lock.close()


@pytest.mark.asyncio
async def test_async_release_force(tmp_path: Path) -> None:
    lock = _make(tmp_path)
    try:
        await lock.acquire_read(timeout=2)
        await lock.release(force=True)
    finally:
        await lock.close()


@pytest.mark.asyncio
async def test_async_release_and_close_skip_when_pid_differs(tmp_path: Path, mocker: MockerFixture) -> None:
    # A pid that no longer matches the creator's must leave the parent's still-held lock untouched.
    lock = _make(tmp_path)
    await lock.acquire_write(timeout=2)
    try:
        mocker.patch("filelock._soft_rw._async.os.getpid", return_value=os.getpid() + 1)
        await lock.release(force=True)
        await lock.close()
        mocker.stopall()
        await lock.release()
    finally:
        mocker.stopall()
        await lock.close()


@pytest.mark.asyncio
async def test_async_leaked_singleton_is_closed_on_teardown(tmp_path: Path) -> None:
    # A live heartbeat thread keeps the singleton reachable, so the autouse teardown finds and closes it.
    path = tmp_path / "singleton.lock"
    lock = AsyncSoftReadWriteLock(str(path), heartbeat_interval=0.5, stale_threshold=1.5, poll_interval=0.02)
    await lock.acquire_write(timeout=2)
    assert path.with_name("singleton.lock.write").exists()


@pytest.mark.asyncio
async def test_async_writer_times_out_behind_reader(tmp_path: Path) -> None:
    reader = _make(tmp_path)
    await reader.acquire_read(timeout=2)
    try:
        writer = _make(tmp_path)
        try:
            with pytest.raises(Timeout):
                await writer.acquire_write(timeout=0.3)
        finally:
            await writer.close()
    finally:
        await reader.release()
        await reader.close()


@pytest.mark.parametrize("mode", [pytest.param("read", id="read"), pytest.param("write", id="write")])
@pytest.mark.asyncio
@XFAIL_WITHOUT_COROUTINE_CANCELLATION
async def test_async_acquire_cancellation_hands_the_claim_back(
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
async def test_async_acquire_cancellation_surfaces_a_failed_acquire(tmp_path: Path, mocker: MockerFixture) -> None:
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
async def test_async_acquire_cancellation_surfaces_a_failed_release(tmp_path: Path, mocker: MockerFixture) -> None:
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
async def test_async_release_cancellation_drains_the_release(tmp_path: Path, mocker: MockerFixture) -> None:
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
async def test_async_release_cancellation_surfaces_a_failed_release(tmp_path: Path, mocker: MockerFixture) -> None:
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
