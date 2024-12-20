"""Read/write file lock."""

from __future__ import annotations

from typing import TYPE_CHECKING

from filelock._unix import NonExclusiveUnixFileLock, UnixFileLock, has_fcntl
from filelock.read_write._api import BaseReadWriteFileLock, ReadWriteMode, _DisabledReadWriteFileLock
from filelock.read_write._wrapper import BaseReadWriteFileLockWrapper, _DisabledReadWriteFileLockWrapper
from filelock.read_write.asyncio import (
    AsyncReadWriteFileLock,
    AsyncReadWriteFileLockWrapper,
    BaseAsyncReadWriteFileLock,
    BaseAsyncReadWriteFileLockWrapper,
    UnixAsyncReadWriteFileLock,
    UnixAsyncReadWriteFileLockWrapper,
)

if TYPE_CHECKING:
    from filelock._api import BaseFileLock

ReadWriteFileLock: type[BaseReadWriteFileLock]
ReadWriteFileLockWrapper: type[BaseReadWriteFileLockWrapper]


class UnixReadWriteFileLock(BaseReadWriteFileLock):
    """Unix implementation of a read/write FileLock."""

    _shared_file_lock_cls: type[BaseFileLock] = NonExclusiveUnixFileLock
    _exclusive_file_lock_cls: type[BaseFileLock] = UnixFileLock


class UnixReadWriteFileLockWrapper(BaseReadWriteFileLockWrapper):
    """Wrapper for a Unix implementation of a read/write FileLock."""

    _read_write_file_lock_cls = UnixReadWriteFileLock


if has_fcntl:  # pragma: win32 no cover
    ReadWriteFileLock = UnixReadWriteFileLock
    ReadWriteFileLockWrapper = UnixReadWriteFileLockWrapper
    has_read_write_file_lock = True
else:  # pragma: win32 cover
    ReadWriteFileLock = _DisabledReadWriteFileLock
    ReadWriteFileLockWrapper = _DisabledReadWriteFileLockWrapper
    has_read_write_file_lock = False


__all__ = [
    "AsyncReadWriteFileLock",
    "AsyncReadWriteFileLockWrapper",
    "BaseAsyncReadWriteFileLock",
    "BaseAsyncReadWriteFileLockWrapper",
    "BaseReadWriteFileLock",
    "BaseReadWriteFileLockWrapper",
    "ReadWriteFileLock",
    "ReadWriteFileLockWrapper",
    "ReadWriteMode",
    "UnixAsyncReadWriteFileLock",
    "UnixAsyncReadWriteFileLockWrapper",
    "UnixReadWriteFileLock",
    "UnixReadWriteFileLockWrapper",
    "has_read_write_file_lock",
]
