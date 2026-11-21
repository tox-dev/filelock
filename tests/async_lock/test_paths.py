from __future__ import annotations

import asyncio
import os
from functools import partial
from typing import TYPE_CHECKING, NamedTuple

import pytest

from filelock import (
    AsyncFileLock,
    AsyncSoftFileLock,
    BaseAsyncFileLock,
    Timeout,
)
from tests.capability_marks import (
    NEEDS_FCNTL,
    NEEDS_PARENT_SYMLINK_COLLAPSE,
)

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.parametrize("lock_type", [AsyncFileLock, AsyncSoftFileLock])
@pytest.mark.asyncio
async def test_async_relative_waiter_keeps_acquisition_directory(
    lock_type: type[BaseAsyncFileLock], working_directories: tuple[Path, Path]
) -> None:
    first, second = working_directories
    holder = lock_type(first / "resource.lock")
    await holder.acquire()
    polling = asyncio.Event()
    contender = lock_type("resource.lock", timeout=0.2, poll_interval=0.001)
    task = asyncio.create_task(contender.acquire(cancel_check=partial(_signal_poll, polling)))
    try:
        await asyncio.wait_for(polling.wait(), timeout=2)
        os.chdir(second)
        with pytest.raises(Timeout):
            await asyncio.wait_for(task, timeout=2)
    finally:
        await holder.release(force=True)

    assert not (second / "resource.lock").exists()


def _signal_poll(polling: asyncio.Event) -> bool:
    polling.set()
    return False


@pytest.mark.asyncio
async def test_async_relative_release_uses_acquisition_directory(
    working_directories: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    first, second = working_directories
    lock = AsyncSoftFileLock("resource.lock")
    await lock.acquire()

    monkeypatch.chdir(second)
    await lock.release()

    assert not (first / "resource.lock").exists()


@pytest.mark.parametrize("lock_type", [AsyncFileLock, AsyncSoftFileLock])
@pytest.mark.asyncio
async def test_async_singleton_held_before_chdir_re_enters_by_absolute_path(
    lock_type: type[BaseAsyncFileLock], working_directories: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    first, second = working_directories
    held = lock_type("resource.lock", is_singleton=True)
    await held.acquire()
    monkeypatch.chdir(second)
    async with lock_type(first / "resource.lock", is_singleton=True) as again:
        assert (again is held, held.lock_counter) == (True, 2)
    await held.release(force=True)


@pytest.mark.parametrize("lock_type", [AsyncFileLock, AsyncSoftFileLock])
@pytest.mark.asyncio
async def test_async_shared_singleton_refuses_moved_relative_path(
    lock_type: type[BaseAsyncFileLock], working_directories: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    first, second = working_directories
    relative = lock_type("resource.lock", is_singleton=True)
    absolute = lock_type(first / "resource.lock", is_singleton=True)
    assert absolute is relative

    monkeypatch.chdir(second)
    with pytest.raises(RuntimeError, match="singleton lock path changed after sharing"):
        await absolute.acquire(timeout=0)

    assert not (second / "resource.lock").exists()
    async with lock_type(first / "resource.lock", is_singleton=True):
        assert (first / "resource.lock").exists()


@NEEDS_PARENT_SYMLINK_COLLAPSE
@pytest.mark.parametrize("lock_type", [AsyncFileLock, AsyncSoftFileLock])
@pytest.mark.asyncio  # pragma: win32 no cover
async def test_symlink_same_canonical_path(tmp_path: Path, lock_type: type[BaseAsyncFileLock]) -> None:
    # A symlinked parent directory resolves to the same canonical key; the final component stays literal.
    real_dir = tmp_path / "real"
    real_dir.mkdir()
    (tmp_path / "link").symlink_to(real_dir)

    async with lock_type(str(real_dir / "test.lock")):
        lock2 = lock_type(str(tmp_path / "link" / "test.lock"))
        with pytest.raises(RuntimeError, match="Deadlock"):
            await lock2.acquire()


@NEEDS_FCNTL
@pytest.mark.parametrize("depth", [1, 2])
@pytest.mark.asyncio  # pragma: needs fcntl
async def test_release_drops_acquisition_key_after_parent_retarget(tmp_path: Path, depth: int) -> None:
    lock_path, original_path, replacement_path = _symlinked_lock_paths(tmp_path)
    lock = AsyncFileLock(lock_path, run_in_executor=False)
    for _depth in range(depth):
        await lock.acquire()
    _retarget_parent(lock_path, replacement_path)

    for _depth in range(depth):
        await lock.release()

    async with AsyncFileLock(original_path, run_in_executor=False) as successor:
        assert successor.is_locked


def _symlinked_lock_paths(tmp_path: Path) -> _SymlinkedPaths:  # pragma: needs fcntl
    original = tmp_path / "original"
    replacement = tmp_path / "replacement"
    original.mkdir()
    replacement.mkdir()
    link = tmp_path / "link"
    link.symlink_to(original, target_is_directory=True)
    return _SymlinkedPaths(link / "test.lock", original / "test.lock", replacement / "test.lock")


class _SymlinkedPaths(NamedTuple):
    lock_path: Path
    original_path: Path
    replacement_path: Path


def _retarget_parent(lock_path: Path, replacement_path: Path) -> None:  # pragma: needs fcntl
    link = lock_path.parent
    link.unlink()
    link.symlink_to(replacement_path.parent, target_is_directory=True)


@NEEDS_FCNTL
@pytest.mark.asyncio  # pragma: needs fcntl
async def test_forced_release_drops_acquisition_key_after_parent_retarget(tmp_path: Path) -> None:
    lock_path, original_path, replacement_path = _symlinked_lock_paths(tmp_path)
    lock = AsyncFileLock(lock_path, run_in_executor=False)
    await lock.acquire()
    await lock.acquire()
    _retarget_parent(lock_path, replacement_path)

    await lock.release(force=True)

    async with AsyncFileLock(original_path, run_in_executor=False) as successor:
        assert successor.is_locked


@NEEDS_FCNTL
@pytest.mark.asyncio
async def test_release_keeps_retargeted_parent_holder_registered(tmp_path: Path) -> None:  # pragma: needs fcntl
    lock_path, _original_path, replacement_path = _symlinked_lock_paths(tmp_path)
    original = AsyncFileLock(lock_path, run_in_executor=False)
    await original.acquire()
    _retarget_parent(lock_path, replacement_path)
    async with AsyncFileLock(replacement_path, run_in_executor=False):
        await original.release()
        with pytest.raises(RuntimeError, match="Deadlock"):
            await AsyncFileLock(replacement_path, run_in_executor=False).acquire(cancel_check=lambda: True)
