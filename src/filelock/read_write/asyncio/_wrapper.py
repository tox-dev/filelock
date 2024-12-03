from __future__ import annotations

from typing import TYPE_CHECKING

from filelock.read_write._api import ReadWriteMode
from filelock.read_write._wrapper import BaseReadWriteFileLockWrapper

if TYPE_CHECKING:
    import asyncio
    import os
    from concurrent import futures

    from ._api import BaseAsyncReadWriteFileLock


class BaseAsyncReadWriteFileLockWrapper(BaseReadWriteFileLockWrapper):
    """
    Convenience wrapper class for async read/write locks.

    Provides `.read()` and `.write()` methods to easily access a read or write lock.

    .. code-block:: python

        # Acquire a non-exclusive reader lock
        async with lock.read():
            pass

        # Acquire an exclusive writer lock
        async with lock.write():
            pass
    """

    _read_write_file_lock_cls: type[BaseAsyncReadWriteFileLock]

    def __init__(  # noqa: PLR0913
        self,
        lock_file: str | os.PathLike[str] | None = None,
        timeout: float = -1,
        mode: int = 0o644,
        thread_local: bool = False,  # noqa: FBT001, FBT002
        *,
        blocking: bool = True,
        lock_file_inner: str | os.PathLike[str] | None = None,
        lock_file_outer: str | os.PathLike[str] | None = None,
        loop: asyncio.AbstractEventLoop | None = None,
        run_in_executor: bool = True,
        executor: futures.Executor | None = None,
    ) -> None:
        """See documentation of BaseAsyncReadWriteFileLock for parameter descriptions."""
        self.read_lock = self._read_write_file_lock_cls(
            lock_file=lock_file,
            lock_file_inner=lock_file_inner,
            lock_file_outer=lock_file_outer,
            read_write_mode=ReadWriteMode.READ,
            timeout=timeout,
            mode=mode,
            thread_local=thread_local,
            blocking=blocking,
            loop=loop,
            run_in_executor=run_in_executor,
            executor=executor,
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
            loop=loop,
            run_in_executor=run_in_executor,
            executor=executor,
        )


class _DisabledAsyncReadWriteFileLockWrapper(BaseAsyncReadWriteFileLockWrapper):
    def __init__(  # noqa: PLR0913
        self,
        lock_file: str | os.PathLike[str] | None = None,
        timeout: float = -1,
        mode: int = 0o644,
        thread_local: bool = False,  # noqa: FBT001, FBT002
        *,
        blocking: bool = True,
        lock_file_inner: str | os.PathLike[str] | None = None,
        lock_file_outer: str | os.PathLike[str] | None = None,
        loop: asyncio.AbstractEventLoop | None = None,
        run_in_executor: bool = True,
        executor: futures.Executor | None = None,
    ) -> None:
        msg = "AsyncReadWriteFileLock is unavailable."
        raise NotImplementedError(msg)


__all__ = [
    "BaseAsyncReadWriteFileLockWrapper",
]
