from __future__ import annotations

import filelock.asyncio


def test_asyncio_module_dir_lists_only_its_public_api() -> None:
    assert dir(filelock.asyncio) == sorted(filelock.asyncio.__all__)


def test_async_context_classes_are_exported() -> None:
    assert {"AsyncFileLockContext", "AsyncFileLockMeta", "AsyncThreadLocalFileContext"} <= set(filelock.asyncio.__all__)


def test_async_file_lock_subclasses_file_lock() -> None:
    assert {filelock.FileLock, filelock.BaseAsyncFileLock} <= set(filelock.AsyncFileLock.__mro__)


def test_current_context_exposes_the_transition_gate() -> None:
    local = filelock.asyncio.AsyncThreadLocalFileContext(
        lock_file="lock", timeout=2, mode=0o600, blocking=True, poll_interval=0.1
    )
    assert isinstance(local.current, filelock.asyncio.AsyncFileLockContext)
    assert isinstance(local.current.transition_gate, filelock.asyncio.AsyncTransitionGate)
