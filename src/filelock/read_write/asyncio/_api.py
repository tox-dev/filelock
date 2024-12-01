from __future__ import annotations

import time
from typing import TYPE_CHECKING

from filelock.asyncio import AsyncAcquireReturnProxy, BaseAsyncFileLock
from filelock.read_write._api import BaseReadWriteFileLock, ReadWriteMode

if TYPE_CHECKING:
    import os
    import sys
    from types import TracebackType

    if sys.version_info >= (3, 11):  # pragma: no cover (py311+)
        from typing import Self
    else:  # pragma: no cover (<py311)
        from typing_extensions import Self


class BaseAsyncReadWriteFileLock(BaseReadWriteFileLock):
    """
    An asynchronous, writer-preferring read/write file lock.

    Readers share the lock in READ mode (multiple readers at once).
    Writers get an exclusive lock in WRITE mode and block both readers and other writers.
    Writers have priority: if a writer arrives, new readers must wait until the writer finishes.
    """

    _shared_file_lock_cls: type[BaseAsyncFileLock]
    _exclusive_file_lock_cls: type[BaseAsyncFileLock]

    def __init__(  # noqa: PLR0913
        self,
        read_write_mode: ReadWriteMode,
        lock_file: str | os.PathLike[str] | None = None,
        timeout: float = -1,
        mode: int = 0o644,
        *,
        blocking: bool = True,
        lock_file_inner: str | os.PathLike[str] | None = None,
        lock_file_outer: str | os.PathLike[str] | None = None,
    ) -> None:
        """
        Create a new async writer-preferring read/write lock object. Multiple READers can hold the lock
        at the same time, but a WRITEr is guaranteed to hold the lock exclusively across both
        readers and writers.

        This object will use two lock files to ensure writers have priority over readers.

        Note that this lock is always thread-local, to allow for non-exclusive access.

        :param read_write_mode: whether this object should be in WRITE mode or READ mode.
        :param lock_file: path to the file. Note that two files will be created: \
            ``{lock_file}.inner`` and ``{lock_file}.outer``. \
            If not specified, ``lock_file_inner`` and ``lock_file_outer`` must both be specified.
        :param timeout: default timeout when acquiring the lock, in seconds. It will be used as fallback value in \
            the acquire method, if no timeout value (``None``) is given. If you want to disable the timeout, set it \
            to a negative value. A timeout of 0 means that there is exactly one attempt to acquire the file lock.
        :param mode: file permissions for the lockfile
        :param blocking: whether the lock should be blocking or not
        :param lock_file_inner: path to the inner lock file. Can be left unspecified if ``lock_file`` is specified.
        :param lock_file_outer: path to the outer lock file Can be left unspecified if ``lock_file`` is specified.
        """
        super().__init__(
            read_write_mode=read_write_mode,
            lock_file=lock_file,
            timeout=timeout,
            mode=mode,
            thread_local=True,
            blocking=blocking,
            lock_file_inner=lock_file_inner,
            lock_file_outer=lock_file_outer,
        )

        self._inner_lock = (
            self._shared_file_lock_cls(
                self.lock_file_inner,
                timeout=self._inner_lock.timeout,
                mode=self._inner_lock.mode,
                thread_local=True,
                blocking=self._inner_lock.blocking,
                is_singleton=False,
            )
            if self.read_write_mode == ReadWriteMode.READ
            else self._exclusive_file_lock_cls(
                self.lock_file_inner,
                timeout=self._inner_lock.timeout,
                mode=self._inner_lock.mode,
                thread_local=True,
                blocking=self._inner_lock.blocking,
                is_singleton=False,
            )
        )

        self._outer_lock = self._exclusive_file_lock_cls(
            self.lock_file_outer,
            timeout=self._outer_lock.timeout,
            mode=self._outer_lock.mode,
            thread_local=True,
            blocking=self._outer_lock.blocking,
            is_singleton=False,
        )

    async def acquire(
        self,
        timeout: float | None = None,
        poll_interval: float = 0.05,
        *,
        blocking: bool | None = None,
    ) -> AsyncAcquireReturnProxy:
        """
        Try to acquire the file lock.

        :param timeout: maximum wait time for acquiring the lock, ``None`` means use the default
            :attr:`~BaseFileLock.timeout` is and if ``timeout < 0``, there is no timeout and
            this method will block until the lock could be acquired
        :param poll_interval: interval of trying to acquire the lock file
        :param blocking: defaults to True. If False, function will return immediately if it cannot obtain a lock on the
         first attempt. Otherwise, this method will block until the timeout expires or the lock is acquired.
        :raises Timeout: if fails to acquire lock within the timeout period
        :return: a context object that will unlock the file when the context is exited

        .. code-block:: python

            # You can use this method in the context manager (recommended)
            with lock.acquire():
                pass

            # Or use an equivalent try-finally construct:
            lock.acquire()
            try:
                pass
            finally:
                lock.release()

        """
        start_time = time.monotonic()
        # Writers or readers must first acquire the outer lock to verify no writer is active or pending.
        await self._outer_lock.acquire(timeout=timeout, poll_interval=poll_interval, blocking=blocking)
        dur = time.monotonic() - start_time
        if timeout is not None:
            timeout -= dur

        if self.read_write_mode == ReadWriteMode.READ:
            try:
                # Acquire the inner lock for reading.
                await self._inner_lock.acquire(timeout=timeout, poll_interval=poll_interval, blocking=blocking)
            finally:
                # Release outer lock once the inner lock is acquired, allowing other readers in.
                await self._outer_lock.release()
        else:
            # In write mode, hold both locks:
            # - Outer lock prevents new readers from starting.
            # - Inner lock ensures exclusive write access.
            await self._inner_lock.acquire(timeout=timeout, poll_interval=poll_interval, blocking=blocking)
        return AsyncAcquireReturnProxy(lock=self)

    async def release(self, *, force: bool = False) -> None:
        """
        Releases the file lock. Please note, that the lock is only completely released, if the lock counter is 0.
        Also note, that the lock file itself is not automatically deleted.

        :param force: If true, the lock counter is ignored and the lock is released in every case/

        """
        await self._inner_lock.release(force=force)
        if self.read_write_mode == ReadWriteMode.WRITE:
            await self._outer_lock.release(force=force)

    async def __aenter__(self) -> Self:
        """
        Acquire the lock.

        :return: the lock object

        """
        await self.acquire()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """
        Release the lock.

        :param exc_type: the exception type if raised
        :param exc_value: the exception value if raised
        :param traceback: the exception traceback if raised

        """
        await self.release()

    def __enter__(self) -> None:
        """
        Replace old __enter__ method to avoid using it.

        NOTE: DO NOT USE `with` FOR ASYNCIO LOCKS, USE `async with` INSTEAD.

        :return: none
        :rtype: NoReturn
        """
        msg = "Do not use `with` for asyncio locks, use `async with` instead."
        raise NotImplementedError(msg)

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        pass


class _DisabledAsyncReadWriteFileLock(BaseAsyncReadWriteFileLock):
    def __new__(cls) -> None:
        msg = "AsyncReadWriteFileLock is unavailable."
        raise NotImplementedError(msg)


__all__ = [
    "BaseAsyncReadWriteFileLock",
    "_DisabledAsyncReadWriteFileLock",
]
