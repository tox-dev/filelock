"""An asyncio-based implementation of the file lock."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time
from dataclasses import dataclass
from inspect import iscoroutinefunction
from threading import local
from typing import TYPE_CHECKING, Any, Final, NoReturn, TypeVar

from ._api import (
    _UNSET_FILE_MODE,
    BaseFileLock,
    CloseErrorPolicy,
    ContextErrorPolicy,
    FileLockContext,
    FileLockMeta,
    _canonical,
    _raise_body_and_release,
)
from ._error import Timeout
from ._soft import SoftFileLock
from ._unix import UnixFileLock
from ._windows import WindowsFileLock

if TYPE_CHECKING:
    import sys
    from collections.abc import Callable
    from concurrent import futures
    from types import TracebackType

    if sys.version_info >= (3, 11):  # pragma: no cover (py311+)
        from typing import Self
    else:  # pragma: no cover (<py311)
        from typing_extensions import Self


_LOGGER: Final[logging.Logger] = logging.getLogger("filelock")

_AT = TypeVar("_AT", bound="BaseAsyncFileLock")


class AsyncFileLockMeta(FileLockMeta):
    def __call__(  # ty: ignore[invalid-method-override]  # noqa: PLR0913
        cls: type[_AT],  # noqa: N805
        lock_file: str | os.PathLike[str],
        timeout: float = -1,
        mode: int = _UNSET_FILE_MODE,
        thread_local: bool = False,  # noqa: FBT001, FBT002
        *,
        blocking: bool = True,
        is_singleton: bool = False,
        poll_interval: float = 0.05,
        lifetime: float | None = None,
        context_error_policy: ContextErrorPolicy = "chain",
        close_error_policy: CloseErrorPolicy = "default",
        fallback_to_soft: bool = True,
        loop: asyncio.AbstractEventLoop | None = None,
        run_in_executor: bool = True,
        executor: futures.Executor | None = None,
    ) -> _AT:
        if thread_local and run_in_executor:
            msg = "run_in_executor is not supported when thread_local is True"
            raise ValueError(msg)
        return super().__call__(
            lock_file=lock_file,
            timeout=timeout,
            mode=mode,
            thread_local=thread_local,
            blocking=blocking,
            is_singleton=is_singleton,
            poll_interval=poll_interval,
            lifetime=lifetime,
            context_error_policy=context_error_policy,
            close_error_policy=close_error_policy,
            fallback_to_soft=fallback_to_soft,
            loop=loop,
            run_in_executor=run_in_executor,
            executor=executor,
        )


class BaseAsyncFileLock(BaseFileLock, metaclass=AsyncFileLockMeta):
    """
    Base class for asynchronous file locks.

    .. versionadded:: 3.15.0

    """

    _deadlock_holder_desc: str = "BaseAsyncFileLock instance in this task"

    def __init__(  # noqa: PLR0913
        self,
        lock_file: str | os.PathLike[str],
        timeout: float = -1,
        mode: int = _UNSET_FILE_MODE,
        thread_local: bool = False,  # noqa: FBT001, FBT002
        *,
        blocking: bool = True,
        is_singleton: bool = False,
        poll_interval: float = 0.05,
        lifetime: float | None = None,
        context_error_policy: ContextErrorPolicy = "chain",
        close_error_policy: CloseErrorPolicy = "default",
        fallback_to_soft: bool = True,
        loop: asyncio.AbstractEventLoop | None = None,
        run_in_executor: bool = True,
        executor: futures.Executor | None = None,
    ) -> None:
        """
        Create a new lock object.

        :param lock_file: path to the file
        :param timeout: default timeout when acquiring the lock, in seconds. It will be used as fallback value in the
            acquire method, if no timeout value (``None``) is given. If you want to disable the timeout, set it to a
            negative value. A timeout of 0 means that there is exactly one attempt to acquire the file lock.
        :param mode: file permissions for the lockfile. When not specified, the OS controls permissions via umask and
            default ACLs, preserving POSIX default ACL inheritance in shared directories.
        :param thread_local: Whether this object's internal context should be thread local or not. If this is set to
            ``False`` then the lock will be reentrant across threads. When ``True`` (the default), **all fields of the
            lock's internal context are per-thread**, including the configuration values ``poll_interval``, ``timeout``,
            ``blocking``, ``mode``, and ``lifetime``. Setting one of these properties from one thread does not change
            the value seen by another thread; threads that did not perform the write continue to see the value supplied
            at construction time. If you need configuration values to be visible across threads, construct the lock
            with ``thread_local=False``.
        :param blocking: whether the lock should be blocking or not
        :param is_singleton: If this is set to ``True`` then only one instance of this class will be created per lock
            file. This is useful if you want to use the lock object for reentrant locking without needing to pass the
            same object around.
        :param poll_interval: default interval for polling the lock file, in seconds. It will be used as fallback value
            in the acquire method, if no poll_interval value (``None``) is given.
        :param lifetime: for :class:`AsyncSoftFileLock`, the maximum time in seconds a lock may be held before it
            expires: a waiting process breaks a lock file whose modification time is older than ``lifetime`` seconds,
            even if the holder is still alive. ``None`` (the default) means locks never expire. Native OS locks
            (:class:`AsyncFileLock`) cannot be revoked by file age and ignore a non-``None`` ``lifetime``, with a
            warning.
        :param context_error_policy: how a context manager reconciles a failure in its body with a failure while
            releasing on exit. ``"chain"`` (the default) keeps Python's behavior: the release error propagates with the
            body error in its ``__context__``. ``"group"`` raises a :class:`BaseExceptionGroup` holding the body error
            first and the release error second, so neither hides the other.
        :param close_error_policy: for native locks (:class:`AsyncFileLock`), what to do with an ``os.close`` failure
            after the OS unlock has already committed. ``"default"`` keeps each platform's historical behavior,
            ``"raise"`` always propagates the ``OSError``, and ``"suppress"`` always ignores it.
        :param fallback_to_soft: for :class:`AsyncFileLock`, whether to fall back to soft existence locking when
            ``flock`` returns ``ENOSYS``. ``True`` (default) keeps the fallback; ``False`` propagates the error.
        :param loop: The event loop to use. If not specified, the running event loop will be used.
        :param run_in_executor: If this is set to ``True`` then the lock will be acquired in an executor.
        :param executor: The executor to use. If not specified, the default executor will be used.

        """
        self._is_thread_local = thread_local
        self._is_singleton = is_singleton
        self._context_error_policy = context_error_policy  # already validated by the metaclass
        self._close_error_policy = close_error_policy  # already validated by the metaclass
        self._fallback_to_soft = fallback_to_soft

        # External code goes through this class's properties, not the context directly.
        kwargs: dict[str, Any] = {
            "lock_file": os.fspath(lock_file),
            "timeout": timeout,
            "mode": mode,
            "blocking": blocking,
            "poll_interval": poll_interval,
            "lifetime": lifetime,
            "loop": loop,
            "run_in_executor": run_in_executor,
            "executor": executor,
        }
        self._context: AsyncFileLockContext = (AsyncThreadLocalFileContext if thread_local else AsyncFileLockContext)(
            **kwargs
        )

    @property
    def run_in_executor(self) -> bool:
        """Whether run in executor."""
        return self._context.run_in_executor

    @property
    def executor(self) -> futures.Executor | None:
        """The executor."""
        return self._context.executor

    @executor.setter
    def executor(self, value: futures.Executor | None) -> None:  # pragma: no cover
        """
        Change the executor.

        :param futures.Executor | None value: the new executor or ``None``

        """
        self._context.executor = value

    @property
    def loop(self) -> asyncio.AbstractEventLoop | None:
        """The event loop."""
        return self._context.loop

    async def acquire(  # ty: ignore[invalid-method-override]
        self,
        timeout: float | None = None,
        poll_interval: float | None = None,
        *,
        blocking: bool | None = None,
        cancel_check: Callable[[], bool] | None = None,
    ) -> AsyncAcquireReturnProxy:
        """
        Try to acquire the file lock.

        :param timeout: maximum wait time for acquiring the lock, ``None`` means use the default
            :attr:`~BaseFileLock.timeout` is and if ``timeout < 0``, there is no timeout and this method will block
            until the lock could be acquired
        :param poll_interval: interval of trying to acquire the lock file, ``None`` means use the default
            :attr:`~BaseFileLock.poll_interval`
        :param blocking: defaults to True. If False, function will return immediately if it cannot obtain a lock on the
            first attempt. Otherwise, this method will block until the timeout expires or the lock is acquired.
        :param cancel_check: a callable returning ``True`` when the acquisition should be canceled. Checked on each poll
            iteration. When triggered, raises :class:`~Timeout` just like an expired timeout.

        :returns: a context object that will unlock the file when the context is exited

        :raises Timeout: if fails to acquire lock within the timeout period

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
        if timeout is None:
            timeout = self._context.timeout

        if blocking is None:
            blocking = self._context.blocking

        if poll_interval is None:
            poll_interval = self._context.poll_interval

        # Bump early; _undo_acquire rolls it back if acquisition fails.
        self._context.lock_counter += 1

        canonical = _canonical(self.lock_file)
        self._raise_if_would_deadlock(canonical, timeout=timeout, blocking=blocking)

        try:
            await self._async_poll_until_acquired(
                blocking=blocking,
                cancel_check=cancel_check,
                timeout=timeout,
                poll_interval=poll_interval,
                start_time=time.perf_counter(),
            )
        except BaseException:
            self._undo_acquire(canonical)
            raise
        self._commit_acquire(canonical)
        return AsyncAcquireReturnProxy(lock=self)

    async def _async_poll_until_acquired(
        self,
        *,
        blocking: bool,
        cancel_check: Callable[[], bool] | None,
        timeout: float,
        poll_interval: float,
        start_time: float,
    ) -> None:
        lock_id = id(self)
        lock_filename = self.lock_file
        while True:
            if not self.is_locked:
                self._try_break_expired_lock()
                _LOGGER.debug("Attempting to acquire lock %s on %s", lock_id, lock_filename)
                await self._run_internal_method(self._acquire)
            if self.is_locked:
                _LOGGER.debug("Lock %s acquired on %s", lock_id, lock_filename)
                return
            if self._check_give_up(
                lock_id,
                lock_filename,
                blocking=blocking,
                cancel_check=cancel_check,
                timeout=timeout,
                start_time=start_time,
            ):
                raise Timeout(lock_filename)
            _LOGGER.debug("Lock %s not acquired on %s, waiting %s seconds ...", lock_id, lock_filename, poll_interval)
            await asyncio.sleep(poll_interval)

    async def release(self, force: bool = False) -> None:  # ty: ignore[invalid-method-override]  # noqa: FBT001, FBT002
        """
        Release the file lock. The lock is only completely released when the lock counter reaches 0. The lock file
        itself may be deleted automatically, the behavior is platform-specific.

        :param force: If true, the lock counter is ignored and the lock is released in every case.

        """
        if not self.is_locked:
            return
        if not force and self._context.lock_counter > 1:
            self._context.lock_counter -= 1
            return

        lock_id, lock_filename = id(self), self.lock_file
        _LOGGER.debug("Attempting to release lock %s on %s", lock_id, lock_filename)
        # Run release as its own task and shield it: a cancellation arriving mid-release must not stop the state
        # transition halfway. On cancellation, wait for the task to finish before letting the cancel reach the caller.
        release_task = asyncio.ensure_future(self._run_internal_method(self._release))
        try:
            await asyncio.shield(release_task)
        except asyncio.CancelledError:
            with contextlib.suppress(Exception):
                await release_task
            self._commit_release_if_released()
            raise
        except Exception:
            self._commit_release_if_released()
            raise
        self._commit_release()
        _LOGGER.debug("Lock %s released on %s", lock_id, lock_filename)

    def _commit_release_if_released(self) -> None:
        # Commit only when the backend actually unlocked (close or unlink failed after the OS unlock). If the lock is
        # still held, keep the counter so a later release can retry.
        if not self.is_locked:
            self._commit_release()

    async def _run_internal_method(self, method: Callable[[], Any]) -> None:
        if iscoroutinefunction(method):
            await method()
        elif self.run_in_executor:
            await asyncio.get_running_loop().run_in_executor(self.executor, method)
        else:
            method()

    def __enter__(self) -> NoReturn:
        """Sync context manager entry is not supported because lock acquisition is a coroutine."""
        msg = "Use `async with` — acquire/release are coroutines and cannot be awaited in a sync context manager."
        raise NotImplementedError(msg)

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: object,
    ) -> None:
        """Sync context manager exit is not supported because lock release is a coroutine."""
        msg = "Use `async with` — acquire/release are coroutines and cannot be awaited in a sync context manager."
        raise NotImplementedError(msg)

    async def __aenter__(self) -> Self:
        """
        Acquire the lock.

        :returns: the lock object

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
        Release the lock, reconciling a release failure with any body failure per :attr:`context_error_policy`.

        :param exc_type: the exception type if raised
        :param exc_value: the exception value if raised
        :param traceback: the exception traceback if raised

        """
        await self._release_in_context(exc_value)

    async def _release_in_context(  # ty: ignore[invalid-method-override]
        self, body_error: BaseException | None
    ) -> None:
        # The async counterpart of BaseFileLock._release_in_context: await release, then apply the same policy.
        try:
            await self.release()
        except BaseException as release_error:
            if body_error is None or self._context_error_policy == "chain":
                raise
            _raise_body_and_release(body_error, release_error)

    def __del__(self) -> None:
        """Release on deletion — safe to call during GC even when no event loop is running."""
        with contextlib.suppress(Exception):
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = self._context.loop if self._context.loop and not self._context.loop.is_closed() else None
            if loop is None:
                return
            if not loop.is_running():  # pragma: no cover
                loop.run_until_complete(self.release(force=True))
            else:
                loop.create_task(self.release(force=True))


@dataclass
class AsyncFileLockContext(FileLockContext):
    """A dataclass which holds the context for a ``BaseAsyncFileLock`` object."""

    #: Whether run in executor
    run_in_executor: bool = True

    #: The executor
    executor: futures.Executor | None = None

    #: The loop
    loop: asyncio.AbstractEventLoop | None = None


class AsyncThreadLocalFileContext(AsyncFileLockContext, local):
    """A thread local version of the ``FileLockContext`` class."""


class AsyncAcquireReturnProxy:
    """A context-aware object that will release the lock file when exiting."""

    def __init__(self, lock: BaseAsyncFileLock) -> None:  # noqa: D107
        self.lock = lock

    async def __aenter__(self) -> BaseAsyncFileLock:  # noqa: D105
        return self.lock

    async def __aexit__(  # noqa: D105
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.lock._release_in_context(exc_value)  # noqa: SLF001


class AsyncSoftFileLock(SoftFileLock, BaseAsyncFileLock):
    """Simply watches the existence of the lock file."""


class AsyncUnixFileLock(UnixFileLock, BaseAsyncFileLock):
    """Uses the :func:`fcntl.flock` to hard lock the lock file on unix systems."""


class AsyncWindowsFileLock(WindowsFileLock, BaseAsyncFileLock):
    """Uses the :func:`msvcrt.locking` to hard lock the lock file on windows systems."""


__all__ = [
    "AsyncAcquireReturnProxy",
    "AsyncSoftFileLock",
    "AsyncUnixFileLock",
    "AsyncWindowsFileLock",
    "BaseAsyncFileLock",
]
