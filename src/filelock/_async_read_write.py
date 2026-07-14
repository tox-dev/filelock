"""Async wrapper around :class:`ReadWriteLock` for use with ``asyncio``."""

from __future__ import annotations

import asyncio
import functools
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, ParamSpec, TypeVar

from ._api import _append_exception_context, _raise_chained_errors
from ._async import _BackendOutcome, _capture_call, _drain_future, _future_result, _wait_until_done
from ._read_write import ReadWriteLock

if TYPE_CHECKING:
    import os
    from collections.abc import AsyncGenerator, Callable
    from concurrent import futures
    from types import TracebackType
    from typing import NoReturn

    from ._api import AcquireReturnProxy

_P = ParamSpec("_P")
_R = TypeVar("_R")


class AsyncReadWriteLock:
    """
    Async wrapper around :class:`ReadWriteLock` for use in ``asyncio`` applications.

    This wrapper dispatches every blocking SQLite operation to a thread pool via ``loop.run_in_executor()`` because
    Python's :mod:`sqlite3` module has no async API. It delegates reentrancy, upgrade/downgrade rules, and singleton
    behavior to the underlying :class:`ReadWriteLock`.

    :param lock_file: path to the SQLite database file used as the lock
    :param timeout: maximum wait time in seconds; ``-1`` means block indefinitely
    :param blocking: if ``False``, raise :class:`~filelock.Timeout` immediately when the lock is unavailable
    :param is_singleton: if ``True``, reuse existing :class:`ReadWriteLock` instances for the same resolved path
    :param loop: event loop for ``run_in_executor``; ``None`` uses the running loop
    :param executor: executor for ``run_in_executor``. When ``None`` this lock creates and owns a dedicated
        single-thread executor so every operation runs on the same thread (SQLite affinity requires this) and shuts it
        down in :meth:`close`. This lock uses a caller-supplied executor as-is and never shuts it down, so after passing
        no executor call :meth:`close` to release the owned one.

    .. versionadded:: 3.21.0

    """

    def __init__(  # noqa: PLR0913
        self,
        lock_file: str | os.PathLike[str],
        timeout: float = -1,
        *,
        blocking: bool = True,
        is_singleton: bool = True,
        loop: asyncio.AbstractEventLoop | None = None,
        executor: futures.Executor | None = None,
    ) -> None:
        self._lock = ReadWriteLock(lock_file, timeout, blocking=blocking, is_singleton=is_singleton)
        self._loop = loop
        self._owns_executor = executor is None
        self._executor = executor or ThreadPoolExecutor(max_workers=1)

    @property
    def lock_file(self) -> str:
        """The path to the lock file."""
        return self._lock.lock_file

    @property
    def timeout(self) -> float:
        """The default timeout."""
        return self._lock.timeout

    @property
    def blocking(self) -> bool:
        """Whether blocking is enabled by default."""
        return self._lock.blocking

    @property
    def loop(self) -> asyncio.AbstractEventLoop | None:
        """The event loop (or ``None`` for the running loop)."""
        return self._loop

    @property
    def executor(self) -> futures.Executor:
        """The executor used for ``run_in_executor`` (a dedicated single-thread one if none was supplied)."""
        return self._executor

    @asynccontextmanager
    async def read_lock(self, timeout: float | None = None, *, blocking: bool | None = None) -> AsyncGenerator[None]:
        """
        Async context manager that acquires and releases a shared read lock.

        Falls back to instance defaults for *timeout* and *blocking* when ``None``.

        :param timeout: maximum wait time in seconds, or ``None`` to use the instance default
        :param blocking: if ``False``, raise :class:`~filelock.Timeout` immediately; ``None`` uses the instance default

        """
        if timeout is None:
            timeout = self._lock.timeout
        if blocking is None:
            blocking = self._lock.blocking
        await self.acquire_read(timeout, blocking=blocking)
        body_error: BaseException | None = None
        try:
            yield
        except BaseException as error:
            body_error = error
            raise
        finally:
            await self._release_in_context(body_error)

    @asynccontextmanager
    async def write_lock(self, timeout: float | None = None, *, blocking: bool | None = None) -> AsyncGenerator[None]:
        """
        Async context manager that acquires and releases an exclusive write lock.

        Falls back to instance defaults for *timeout* and *blocking* when ``None``.

        :param timeout: maximum wait time in seconds, or ``None`` to use the instance default
        :param blocking: if ``False``, raise :class:`~filelock.Timeout` immediately; ``None`` uses the instance default

        """
        if timeout is None:
            timeout = self._lock.timeout
        if blocking is None:
            blocking = self._lock.blocking
        await self.acquire_write(timeout, blocking=blocking)
        body_error: BaseException | None = None
        try:
            yield
        except BaseException as error:
            body_error = error
            raise
        finally:
            await self._release_in_context(body_error)

    async def _release_in_context(self, body_error: BaseException | None) -> None:
        try:
            await self.release()
        except BaseException as release_error:
            if body_error is not None:
                _append_exception_context(release_error, body_error)
            raise

    async def acquire_read(self, timeout: float = -1, *, blocking: bool = True) -> AsyncAcquireReadWriteReturnProxy:
        """
        Acquire a shared read lock.

        See :meth:`ReadWriteLock.acquire_read` for full semantics.

        :param timeout: maximum wait time in seconds; ``-1`` means block indefinitely
        :param blocking: if ``False``, raise :class:`~filelock.Timeout` immediately when the lock is unavailable

        :returns: a proxy that can be used as an async context manager to release the lock

        :raises RuntimeError: if a write lock is already held on this instance
        :raises Timeout: if the lock cannot be acquired within *timeout* seconds

        If rollback fails, peers may remain blocked. A later acquisition first retries that cleanup;
        ``release(force=True)`` retries it without acquiring.

        """
        await self._run_acquire(functools.partial(self._lock.acquire_read, timeout, blocking=blocking))
        return AsyncAcquireReadWriteReturnProxy(lock=self)

    async def acquire_write(self, timeout: float = -1, *, blocking: bool = True) -> AsyncAcquireReadWriteReturnProxy:
        """
        Acquire an exclusive write lock.

        See :meth:`ReadWriteLock.acquire_write` for full semantics.

        :param timeout: maximum wait time in seconds; ``-1`` means block indefinitely
        :param blocking: if ``False``, raise :class:`~filelock.Timeout` immediately when the lock is unavailable

        :returns: a proxy that can be used as an async context manager to release the lock

        :raises RuntimeError: if a read lock is already held, or a write lock is held by a different thread
        :raises Timeout: if the lock cannot be acquired within *timeout* seconds

        If rollback fails, peers may remain blocked. A later acquisition first retries that cleanup;
        ``release(force=True)`` retries it without acquiring.

        """
        await self._run_acquire(functools.partial(self._lock.acquire_write, timeout, blocking=blocking))
        return AsyncAcquireReadWriteReturnProxy(lock=self)

    async def release(self, *, force: bool = False) -> None:
        """
        Release one level of the current lock.

        See :meth:`ReadWriteLock.release` for full semantics.

        :param force: if ``True``, release the lock completely regardless of the current lock level and retry cleanup
            from a failed acquisition

        :raises RuntimeError: if no lock is currently held and *force* is ``False``

        """
        await self._run(self._lock.release, force=force)

    async def close(self) -> None:
        """
        Release the lock (if held) and close the underlying SQLite connection.

        After calling this method, the lock instance is no longer usable.

        """
        close_future = self._submit(self._lock.close)
        try:
            await _wait_until_done(close_future)
        except asyncio.CancelledError as cancellation:
            try:
                await _drain_future(close_future)
            except BaseException as error:  # noqa: BLE001  # reported with the cancellation below
                self._raise_cancelled_error(cancellation, error)
            self._shutdown_owned_executor()
            raise
        _future_result(close_future)
        self._shutdown_owned_executor()

    async def _run_acquire(self, acquire: Callable[[], AcquireReturnProxy]) -> None:
        acquire_future = self._submit(acquire)
        try:
            await _wait_until_done(acquire_future)
        except asyncio.CancelledError as cancellation:
            try:
                await _drain_future(acquire_future)
            except asyncio.CancelledError as acquire_error:
                self._raise_cancelled_error(cancellation, acquire_error)
            except BaseException as error:  # noqa: BLE001  # reported with the cancellation below
                self._raise_cancelled_error(cancellation, error)
            try:
                await _drain_future(self._submit(self._lock.release))
            except BaseException as error:  # noqa: BLE001  # reported with the cancellation below
                self._raise_cancelled_error(cancellation, error)
            raise
        _future_result(acquire_future)

    async def _run(self, func: Callable[_P, _R], *args: _P.args, **kwargs: _P.kwargs) -> _R:
        future = self._submit(func, *args, **kwargs)
        try:
            await _wait_until_done(future)
        except asyncio.CancelledError as cancellation:
            try:
                await _drain_future(future)
            except BaseException as error:  # noqa: BLE001  # reported with the cancellation below
                self._raise_cancelled_error(cancellation, error)
            raise
        return _future_result(future)

    def _submit(
        self, func: Callable[_P, _R], *args: _P.args, **kwargs: _P.kwargs
    ) -> asyncio.Future[_BackendOutcome[_R]]:
        return (self._loop or asyncio.get_running_loop()).run_in_executor(
            self._executor,
            _capture_call,
            functools.partial(func, *args, **kwargs),
        )

    @staticmethod
    def _raise_cancelled_error(cancellation: asyncio.CancelledError, error: BaseException) -> NoReturn:
        if (context := error.__context__) is not None and context is not cancellation:
            if (cancellation_context := cancellation.__context__) is not None:
                _append_exception_context(context, cancellation_context)
            cancellation.__context__ = context
        error.__context__ = cancellation
        _raise_chained_errors(error)

    def _shutdown_owned_executor(self) -> None:
        if self._owns_executor:
            self._executor.shutdown(wait=False)

    def __del__(self) -> None:
        # Safety net when close() was never called: shut down the executor we own so its worker thread does not
        # outlive the lock. shutdown(wait=False) never blocks.
        if getattr(self, "_owns_executor", False):
            self._executor.shutdown(wait=False)


class AsyncAcquireReadWriteReturnProxy:
    """Context-aware object that releases the async read/write lock on exit."""

    def __init__(self, lock: AsyncReadWriteLock) -> None:
        self.lock = lock

    async def __aenter__(self) -> AsyncReadWriteLock:
        return self.lock

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.lock.release()


__all__ = [
    "AsyncAcquireReadWriteReturnProxy",
    "AsyncReadWriteLock",
]
