from __future__ import annotations

from typing import Type

from filelock._unix import NonExclusiveUnixFileLock, UnixFileLock, has_fcntl

from .._api import BaseFileLock
from ._api import BaseReadWriteFileLock, _DisabledReadWriteFileLock
from ._wrapper import BaseReadWriteFileLockWrapper, _DisabledReadWriteFileLockWrapper

if has_fcntl:

    class UnixReadWriteFileLock(BaseReadWriteFileLock):
        _shared_file_lock_cls: Type[BaseFileLock] = NonExclusiveUnixFileLock
        _exclusive_file_lock_cls: Type[BaseFileLock] = UnixFileLock

    class UnixReadWriteFileLockWrapper(BaseReadWriteFileLockWrapper):
        _read_write_file_lock_cls = UnixReadWriteFileLock

    ReadWriteFileLock = UnixReadWriteFileLock
    ReadWriteFileLockWrapper = UnixReadWriteFileLockWrapper
else:
    ReadWriteFileLock = _DisabledReadWriteFileLock
    ReadWriteFileLockWrapper = _DisabledReadWriteFileLockWrapper


__all__ = [
    "BaseReadWriteFileLock",
    "BaseReadWriteFileLockWrapper",
    "ReadWriteFileLock",
    "ReadWriteFileLockWrapper",
]
