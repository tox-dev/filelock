from __future__ import annotations

from abc import ABC
from typing import TYPE_CHECKING

from ._api import BaseReadWriteFileLock, ReadWriteMode

if TYPE_CHECKING:
    import os


class BaseReadWriteFileLockWrapper(ABC):
    """
    Convenience wrapper for read/write locks.

    Provides `.read()` and `.write()` methods to easily access a read or write lock.

    .. code-block:: python

        # Acquire a non-exclusive reader lock
        with lock.read():
            pass

        # Acquire an exclusive writer lock
        with lock.write():
            pass
    """

    _read_write_file_lock_cls: type[BaseReadWriteFileLock]

    def __init__(  # noqa: PLR0913
        self,
        lock_file: str | os.PathLike[str] | None = None,
        timeout: float = -1,
        mode: int = 0o644,
        thread_local: bool = True,  # noqa: FBT001, FBT002
        *,
        blocking: bool = True,
        lock_file_inner: str | os.PathLike[str] | None = None,
        lock_file_outer: str | os.PathLike[str] | None = None,
    ) -> None:
        """
        Convenience wrapper for read/write locks.

        See ReadWriteFileLock for description of the parameters.
        """
        self.read_lock = self._read_write_file_lock_cls(
            lock_file=lock_file,
            lock_file_inner=lock_file_inner,
            lock_file_outer=lock_file_outer,
            read_write_mode=ReadWriteMode.READ,
            timeout=timeout,
            mode=mode,
            thread_local=thread_local,
            blocking=blocking,
        )
        self.write_lock = self._read_write_file_lock_cls(
            lock_file=lock_file,
            lock_file_inner=lock_file_inner,
            lock_file_outer=lock_file_outer,
            read_write_mode=ReadWriteMode.WRITE,
            timeout=timeout,
            mode=mode,
            thread_local=thread_local,
            blocking=blocking,
        )

    def __call__(self, read_write_mode: ReadWriteMode) -> BaseReadWriteFileLock:
        """
        Get read/write lock object with the specified ``read_write_mode``.

        :param read_write_mode: whether this object should be in WRITE mode or READ mode.
        :return: a lock object in specified ``read_write_mode``.
        """
        if read_write_mode == ReadWriteMode.READ:
            return self.read_lock
        return self.write_lock

    def read(self) -> BaseReadWriteFileLock:
        """
        Get read/write lock object in READ mode.

        :return: a lock object in READ mode.
        """
        return self(ReadWriteMode.READ)

    def write(self) -> BaseReadWriteFileLock:
        """
        Get read/write lock object in WRITE mode.

        :return: a lock object in WRITE mode.
        """
        return self(ReadWriteMode.WRITE)


class _DisabledReadWriteFileLockWrapper(BaseReadWriteFileLockWrapper):
    def __init__(  # noqa: PLR0913
        self,
        lock_file: str | os.PathLike[str] | None = None,
        timeout: float = -1,
        mode: int = 0o644,
        thread_local: bool = True,  # noqa: FBT001, FBT002
        *,
        blocking: bool = True,
        lock_file_inner: str | os.PathLike[str] | None = None,
        lock_file_outer: str | os.PathLike[str] | None = None,
    ) -> None:
        msg = "ReadWriteFileLock is unavailable."
        raise NotImplementedError(msg)


__all__ = [
    "BaseReadWriteFileLockWrapper",
]
