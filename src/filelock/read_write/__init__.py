from __future__ import annotations

from typing import TYPE_CHECKING

from filelock._unix import NonExclusiveUnixFileLock, UnixFileLock, has_fcntl

from ._api import BaseReadWriteFileLock, ReadWriteMode, _DisabledReadWriteFileLock
from ._wrapper import BaseReadWriteFileLockWrapper, _DisabledReadWriteFileLockWrapper

if TYPE_CHECKING:
    from filelock._api import BaseFileLock

if has_fcntl:

    class UnixReadWriteFileLock(BaseReadWriteFileLock):
        _shared_file_lock_cls: type[BaseFileLock] = NonExclusiveUnixFileLock
        _exclusive_file_lock_cls: type[BaseFileLock] = UnixFileLock

    class UnixReadWriteFileLockWrapper(BaseReadWriteFileLockWrapper):
        _read_write_file_lock_cls = UnixReadWriteFileLock

    ReadWriteFileLock = UnixReadWriteFileLock
    ReadWriteFileLockWrapper = UnixReadWriteFileLockWrapper
    has_read_write_file_lock = True
else:
    ReadWriteFileLock = _DisabledReadWriteFileLock
    ReadWriteFileLockWrapper = _DisabledReadWriteFileLockWrapper
    has_read_write_file_lock = True


__all__ = [
    "BaseReadWriteFileLock",
    "ReadWriteFileLockWrapper",
    "ReadWriteFileLock",
    "ReadWriteMode",
    "has_read_write_file_lock",
]
