from __future__ import annotations

import asyncio
import sys
from typing import TYPE_CHECKING

import pytest

from filelock import AsyncFileLock, AsyncSoftFileLock, Timeout

if TYPE_CHECKING:
    from pathlib import Path


unix_only = pytest.mark.skipif(sys.platform == "win32", reason="unix-only symlink test")


@pytest.mark.parametrize("lock_type", [AsyncFileLock, AsyncSoftFileLock])
@pytest.mark.asyncio
async def test_same_task_different_instances_raises(tmp_path: Path, lock_type: type[AsyncFileLock]) -> None:
    """Acquiring a second instance of the same lock from within the same asyncio task must
    raise ``RuntimeError`` synchronously rather than blocking on the OS-level lock primitive
    forever, mirroring the sync ``BaseFileLock.acquire`` deadlock check (#578)."""
    lock_path = tmp_path / "test.lock"
    lock1 = lock_type(lock_path)
    async with lock1:
        lock2 = lock_type(lock_path)
        with pytest.raises(RuntimeError, match="Deadlock"):
            await lock2.acquire()


@pytest.mark.parametrize("lock_type", [AsyncFileLock, AsyncSoftFileLock])
@pytest.mark.asyncio
async def test_finite_timeout_gives_timeout_not_deadlock(tmp_path: Path, lock_type: type[AsyncFileLock]) -> None:
    """When the second instance is acquired with a finite timeout (so deadlock detection is
    disabled by the ``would_block`` guard), the caller should still see ``Timeout`` after
    the timeout expires — not ``RuntimeError``."""
    lock_path = tmp_path / "test.lock"
    lock1 = lock_type(lock_path)
    async with lock1:
        lock2 = lock_type(lock_path, timeout=0.1)
        with pytest.raises(Timeout):
            await lock2.acquire()


@pytest.mark.parametrize("lock_type", [AsyncFileLock, AsyncSoftFileLock])
@pytest.mark.asyncio
async def test_non_blocking_gives_timeout_not_deadlock(tmp_path: Path, lock_type: type[AsyncFileLock]) -> None:
    """With ``blocking=False``, the ``would_block`` guard is False so the deadlock check
    short-circuits; the second instance must raise ``Timeout`` on the first failed attempt."""
    lock_path = tmp_path / "test.lock"
    lock1 = lock_type(lock_path)
    async with lock1:
        lock2 = lock_type(lock_path, blocking=False)
        with pytest.raises(Timeout):
            await lock2.acquire()


@pytest.mark.parametrize("lock_type", [AsyncFileLock, AsyncSoftFileLock])
@pytest.mark.asyncio
async def test_different_paths_no_conflict(tmp_path: Path, lock_type: type[AsyncFileLock]) -> None:
    lock1 = lock_type(tmp_path / "a.lock")
    lock2 = lock_type(tmp_path / "b.lock")
    async with lock1, lock2:
        assert lock1.is_locked
        assert lock2.is_locked


@pytest.mark.parametrize("lock_type", [AsyncFileLock, AsyncSoftFileLock])
@pytest.mark.asyncio
async def test_same_instance_reentrant_works(tmp_path: Path, lock_type: type[AsyncFileLock]) -> None:
    lock = lock_type(tmp_path / "test.lock")
    async with lock:
        async with lock:
            assert lock.is_locked
        assert lock.is_locked
    assert not lock.is_locked


@pytest.mark.parametrize("lock_type", [AsyncFileLock, AsyncSoftFileLock])
@pytest.mark.asyncio
async def test_singleton_avoids_deadlock(tmp_path: Path, lock_type: type[AsyncFileLock]) -> None:
    lock_path = tmp_path / "test.lock"
    lock1 = lock_type(lock_path, is_singleton=True)
    async with lock1:
        lock2 = lock_type(lock_path, is_singleton=True)
        assert lock1 is lock2
        async with lock2:
            assert lock2.is_locked


@pytest.mark.parametrize("lock_type", [AsyncFileLock, AsyncSoftFileLock])
@pytest.mark.asyncio
async def test_different_tasks_no_false_positive(tmp_path: Path, lock_type: type[AsyncFileLock]) -> None:
    """A second acquire from a *different* asyncio task must not trigger the per-task
    deadlock check. It should either succeed (if the first task released) or raise
    ``Timeout`` (if it didn't). The sync test uses a separate thread; in asyncio the
    equivalent is ``asyncio.create_task``."""
    lock_path = tmp_path / "test.lock"
    lock1 = lock_type(lock_path, timeout=0)
    await lock1.acquire()

    # Run the second acquire in a different task; it must not raise RuntimeError.
    error: BaseException | None = None

    async def acquire_other() -> None:
        nonlocal error
        lock2 = lock_type(lock_path, timeout=0)
        try:
            await lock2.acquire()
        except BaseException as exc:
            error = exc

    task = asyncio.create_task(acquire_other())
    await task
    await lock1.release()
    assert not isinstance(error, RuntimeError), "Should not raise RuntimeError in a different task"


@unix_only
@pytest.mark.parametrize("lock_type", [AsyncFileLock, AsyncSoftFileLock])
@pytest.mark.asyncio
async def test_symlink_same_canonical_path(tmp_path: Path, lock_type: type[AsyncFileLock]) -> None:
    """Two paths that resolve to the same canonical file (one being a symlink) must
    detect the deadlock the same way two equal paths do."""
    lock_path = tmp_path / "test.lock"
    symlink_path = tmp_path / "link.lock"
    symlink_path.symlink_to(lock_path)

    lock1 = lock_type(lock_path)
    async with lock1:
        lock2 = lock_type(symlink_path)
        with pytest.raises(RuntimeError, match="Deadlock"):
            await lock2.acquire()


@pytest.mark.parametrize("lock_type", [AsyncFileLock, AsyncSoftFileLock])
@pytest.mark.asyncio
async def test_cleanup_on_release(tmp_path: Path, lock_type: type[AsyncFileLock]) -> None:
    """The internal ``_registry.held`` map is keyed per task; releasing the first lock
    must clear the entry so a subsequent second acquire would not see a stale holder.
    We can't inspect ``_registry`` directly across tasks, but we *can* verify the
    observable behaviour: a second acquire after release succeeds, not deadlocks."""
    lock_path = tmp_path / "test.lock"
    lock1 = lock_type(lock_path)
    await lock1.acquire()
    await lock1.release()

    lock2 = lock_type(lock_path)
    async with lock2:
        assert lock2.is_locked
    assert not lock2.is_locked


@pytest.mark.parametrize("lock_type", [AsyncFileLock, AsyncSoftFileLock])
@pytest.mark.asyncio
async def test_force_release_clears_registry(tmp_path: Path, lock_type: type[AsyncFileLock]) -> None:
    """``release(force=True)`` must also clear the registry entry — otherwise a
    nested acquire/release cycle inside a ``with`` block would leave a stale entry
    pointing at the now-closed lock, and any later deadlock check would see a
    different ``id(self)`` (the new instance) than the registry's stale lock_id
    and incorrectly raise ``RuntimeError``."""
    lock_path = tmp_path / "test.lock"
    lock1 = lock_type(lock_path)
    async with lock1:
        await lock1.acquire()
        assert lock1.lock_counter == 2
    await lock1.release(force=True)

    lock2 = lock_type(lock_path)
    async with lock2:
        assert lock2.is_locked
    assert not lock2.is_locked
