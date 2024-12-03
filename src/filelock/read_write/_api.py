from __future__ import annotations

import contextlib
import time
from abc import ABC
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING

from filelock._api import DEFAULT_POLL_INTERVAL, AcquireReturnProxy, BaseFileLock, LockProtocol

if TYPE_CHECKING:
    import os
    import sys
    from types import TracebackType

    if sys.version_info >= (3, 11):  # pragma: no cover (py311+)
        from typing import Self
    else:  # pragma: no cover (<py311)
        from typing_extensions import Self


class ReadWriteMode(Enum):
    READ = "read"
    WRITE = "write"


class BaseReadWriteFileLock(contextlib.ContextDecorator, LockProtocol, ABC):
    """Abstract base class for a writer-preferring read/write file lock object."""

    _shared_file_lock_cls: type[BaseFileLock]
    _exclusive_file_lock_cls: type[BaseFileLock]

    def __init__(  # noqa: PLR0913
        self,
        read_write_mode: ReadWriteMode,
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
        Create a new writer-preferring read/write lock object. Multiple READers can hold the lock
        at the same time, but a WRITEr is guaranteed to hold the lock exclusively across both
        readers and writers.

        This object will use two lock files to ensure writers have priority over readers.

        :param read_write_mode: whether this object should be in WRITE mode or READ mode.
        :param lock_file: path to the file. Note that two files will be created: \
            ``{lock_file}.inner`` and ``{lock_file}.outer``. \
            If not specified, ``lock_file_inner`` and ``lock_file_outer`` must both be specified.
        :param timeout: default timeout when acquiring the lock, in seconds. It will be used as fallback value in \
            the acquire method, if no timeout value (``None``) is given. If you want to disable the timeout, set it \
            to a negative value. A timeout of 0 means that there is exactly one attempt to acquire the file lock.
        :param mode: file permissions for the lockfile
        :param thread_local: Whether this object's internal context should be thread local or not. If this is set to \
            ``False`` then the lock will be reentrant across threads. Note that misuse of the lock while this argument \
            is set to ``False`` may result in deadlocks due to the non-exclusive nature of the read/write lock.
        :param blocking: whether the lock should be blocking or not
        :param lock_file_inner: path to the inner lock file. Can be left unspecified if ``lock_file`` is specified.
        :param lock_file_outer: path to the outer lock file Can be left unspecified if ``lock_file`` is specified.

        """
        if read_write_mode == ReadWriteMode.READ:
            file_lock_cls = self._shared_file_lock_cls
        else:
            file_lock_cls = self._exclusive_file_lock_cls
        self.read_write_mode = read_write_mode

        if not lock_file_inner:
            if not lock_file:
                msg = "If lock_file is unspecified, both lock_file_inner and lock_file_outer must be specified."
                raise ValueError(msg)
            lock_file_inner = Path(lock_file).with_suffix(".inner")
        if not lock_file_outer:
            if not lock_file:
                msg = "If lock_file is unspecified, both lock_file_inner and lock_file_outer must be specified."
                raise ValueError(msg)
            lock_file_outer = Path(lock_file).with_suffix(".outer")

        # is_singleton is always disabled, as I don't believe it will work
        # correctly with this setup.
        self._inner_lock = file_lock_cls(
            lock_file_inner,
            timeout=timeout,
            mode=mode,
            thread_local=thread_local,
            blocking=blocking,
            is_singleton=False,
        )
        self._outer_lock = file_lock_cls(
            lock_file_outer,
            timeout=timeout,
            mode=mode,
            thread_local=thread_local,
            blocking=blocking,
            is_singleton=False,
        )

    def is_thread_local(self) -> bool:
        """:return: a flag indicating if this lock is thread local or not"""
        return self._inner_lock.is_thread_local()

    @property
    def is_singleton(self) -> bool:
        """:return: a flag indicating if this lock is singleton or not"""
        return self._inner_lock.is_singleton

    @property
    def lock_file_inner(self) -> str:
        """:return: path to the lock file"""
        return self._inner_lock.lock_file

    @property
    def lock_file_outer(self) -> str:
        """:return: path to the lock file"""
        return self._outer_lock.lock_file

    @property
    def timeout(self) -> float:
        """:return: the default timeout value, in seconds"""
        return self._inner_lock.timeout

    @timeout.setter
    def timeout(self, value: float | str) -> None:
        """
        Change the default timeout value.

        :param value: the new value, in seconds

        """
        self._inner_lock.timeout = float(value)
        self._outer_lock.timeout = float(value)

    @property
    def blocking(self) -> bool:
        """:return: whether the locking is blocking or not"""
        return self._inner_lock.blocking

    @blocking.setter
    def blocking(self, value: bool) -> None:
        """
        Change the default blocking value.

        :param value: the new value as bool

        """
        self._inner_lock.blocking = value
        self._outer_lock.blocking = value

    @property
    def mode(self) -> int:
        """:return: the file permissions for the lockfile"""
        return self._inner_lock.mode

    @property
    def is_locked(self) -> bool:
        """:return: A boolean indicating if the lock file is holding the lock currently."""
        return self._inner_lock.is_locked

    @property
    def lock_counter(self) -> int:
        """:return: The number of times this lock has been acquired (but not yet released)."""
        return self._inner_lock.lock_counter + self._outer_lock.lock_counter

    def acquire(
        self,
        timeout: float | None = None,
        poll_interval: float = DEFAULT_POLL_INTERVAL,
        *,
        poll_intervall: float | None = None,
        blocking: bool | None = None,
    ) -> AcquireReturnProxy:
        """
        Try to acquire the file lock.

        :param timeout: maximum wait time for acquiring the lock, ``None`` means use the default :attr:`~timeout` is and
         if ``timeout < 0``, there is no timeout and this method will block until the lock could be acquired
        :param poll_interval: interval of trying to acquire the lock file
        :param poll_intervall: deprecated, kept for backwards compatibility, use ``poll_interval`` instead
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
        self._outer_lock.acquire(
            timeout=timeout, poll_interval=poll_interval, poll_intervall=poll_intervall, blocking=blocking
        )
        dur = time.monotonic() - start_time
        if timeout:
            timeout -= dur
        if self.read_write_mode == ReadWriteMode.READ:
            try:
                self._inner_lock.acquire(
                    timeout=timeout, poll_interval=poll_interval, poll_intervall=poll_intervall, blocking=blocking
                )
            finally:
                self._outer_lock.release()
        else:
            self._inner_lock.acquire(
                timeout=timeout, poll_interval=poll_interval, poll_intervall=poll_intervall, blocking=blocking
            )
        return AcquireReturnProxy(lock=self)

    def release(self, force: bool = False) -> None:  # noqa: FBT001, FBT002
        """
        Releases the file lock. Please note, that the lock is only completely released, if the lock counter is 0.
        Also note, that the lock file itself is not automatically deleted.

        :param force: If true, the lock counter is ignored and the lock is released in every case/

        """
        self._inner_lock.release(force=force)
        if self.read_write_mode == ReadWriteMode.WRITE:
            self._outer_lock.release(force=force)

    def __enter__(self) -> Self:
        """
        Acquire the lock.

        :return: the lock object

        """
        self.acquire()
        return self

    def __exit__(
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
        self.release()

    def __del__(self) -> None:
        """Called when the lock object is deleted."""
        self.release(force=True)


class _DisabledReadWriteFileLock(BaseReadWriteFileLock):
    def __init__(self, *args, **kwargs) -> None:  # type: ignore[no-untyped-def]  # noqa: ANN002, ANN003
        msg = "ReadWriteFileLock is unavailable."
        raise NotImplementedError(msg)


__all__ = [
    "BaseReadWriteFileLock",
    "ReadWriteMode",
]
