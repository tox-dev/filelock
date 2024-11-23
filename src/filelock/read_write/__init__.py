from __future__ import annotations

from typing import TYPE_CHECKING

from filelock._unix import NonExclusiveUnixFileLock, UnixFileLock, has_fcntl

from ._api import BaseReadWriteFileLock, _DisabledReadWriteFileLock

if TYPE_CHECKING:
    from filelock._api import BaseFileLock

if has_fcntl:

    class UnixReadWriteFileLock(BaseReadWriteFileLock):
        _shared_file_lock_cls: type[BaseFileLock] = NonExclusiveUnixFileLock
        _exclusive_file_lock_cls: type[BaseFileLock] = UnixFileLock

    class UnixReadWriteFileLockWrapper(BaseReadWriteFileLockWrapper):
        _read_write_file_lock_cls = UnixReadWriteFileLock

    ReadWriteFileLock = UnixReadWriteFileLock
else:
    ReadWriteFileLock = _DisabledReadWriteFileLock


__all__ = [
    "BaseReadWriteFileLock",
    "ReadWriteFileLock",
]
