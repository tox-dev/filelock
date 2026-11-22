from __future__ import annotations

import filelock.asyncio


def test_asyncio_module_dir_lists_only_its_public_api() -> None:
    assert dir(filelock.asyncio) == sorted(filelock.asyncio.__all__)


def test_async_file_lock_subclasses_file_lock() -> None:
    assert {filelock.FileLock, filelock.BaseAsyncFileLock} <= set(filelock.AsyncFileLock.__mro__)
