from __future__ import annotations

import logging
import pathlib
import sqlite3
import threading
import time
from contextlib import contextmanager
from typing import TYPE_CHECKING, Literal
from weakref import WeakValueDictionary

from filelock._api import AcquireReturnProxy

from ._error import Timeout

if TYPE_CHECKING:
    import os
    from collections.abc import Generator

_LOGGER = logging.getLogger("filelock")

# sqlite3_busy_timeout() accepts a C int, max 2_147_483_647 on 32-bit. Use a lower value to be safe (~23 days).
_MAX_SQLITE_TIMEOUT_MS = 2_000_000_000 - 1


def timeout_for_sqlite(timeout: float, *, blocking: bool, already_waited: float) -> int:
    if blocking is False:
        return 0

    if timeout == -1:
        return _MAX_SQLITE_TIMEOUT_MS

    if timeout < 0:
        msg = "timeout must be a non-negative number or -1"
        raise ValueError(msg)

    if timeout > 0:
        timeout -= already_waited
        timeout = max(timeout, 0)

    timeout_ms = int(timeout * 1000)
    if timeout_ms > _MAX_SQLITE_TIMEOUT_MS or timeout_ms < 0:
        _LOGGER.warning("timeout %s is too large for SQLite, using %s ms instead", timeout, _MAX_SQLITE_TIMEOUT_MS)
        return _MAX_SQLITE_TIMEOUT_MS
    return timeout_ms


class _ReadWriteLockMeta(type):
    """Metaclass that redirects instance creation to get_lock() when is_singleton=True."""

    def __call__(
        cls,
        lock_file: str | os.PathLike[str],
        timeout: float = -1,
        *,
        blocking: bool = True,
        is_singleton: bool = True,
    ) -> ReadWriteLock:
        if is_singleton:
            return cls.get_lock(lock_file, timeout, blocking=blocking)
        return super().__call__(lock_file, timeout, blocking=blocking, is_singleton=is_singleton)


class ReadWriteLock(metaclass=_ReadWriteLockMeta):
    """
    Cross-process read-write lock backed by SQLite.

    Allows concurrent shared readers or a single exclusive writer. The lock is reentrant within the same mode (multiple
    ``acquire_read`` calls nest, as do multiple ``acquire_write`` calls from the same thread), but upgrading from read
    to write or downgrading from write to read raises :class:`RuntimeError`. Write locks are pinned to the thread that
    acquired them.

    By default, ``is_singleton=True``: calling ``ReadWriteLock(path)`` with the same resolved path returns the same
    instance. The lock file must use a ``.db`` extension (SQLite database).

    :param lock_file: path to the SQLite database file used as the lock
    :param timeout: maximum wait time in seconds; ``-1`` means block indefinitely
    :param blocking: if ``False``, raise :class:`~filelock.Timeout` immediately when the lock is unavailable
    :param is_singleton: if ``True``, reuse existing instances for the same resolved path

    .. versionadded:: 3.21.0
    """

    _instances = WeakValueDictionary()
    _instances_lock = threading.Lock()

    @classmethod
    def get_lock(
        cls, lock_file: str | os.PathLike[str], timeout: float = -1, *, blocking: bool = True
    ) -> ReadWriteLock:
        """
        Return the singleton :class:`ReadWriteLock` for *lock_file*.

        :param lock_file: path to the SQLite database file used as the lock
        :param timeout: maximum wait time in seconds; ``-1`` means block indefinitely
        :param blocking: if ``False``, raise :class:`~filelock.Timeout` immediately when the lock is unavailable
        :raises ValueError: if an instance already exists for this path with different *timeout* or *blocking* values
        :return: the singleton lock instance
        """
        normalized = pathlib.Path(lock_file).resolve()
        with cls._instances_lock:
            if normalized not in cls._instances:
                instance = super(_ReadWriteLockMeta, cls).__call__(
                    lock_file, timeout, blocking=blocking, is_singleton=False
                )
                cls._instances[normalized] = instance
            else:
                instance = cls._instances[normalized]

            if instance.timeout != timeout or instance.blocking != blocking:
                msg = (
                    f"Singleton lock created with timeout={instance.timeout}, blocking={instance.blocking},"
                    f" cannot be changed to timeout={timeout}, blocking={blocking}"
                )
                raise ValueError(msg)
            return instance

    def __init__(
        self,
        lock_file: str | os.PathLike[str],
        timeout: float = -1,
        *,
        blocking: bool = True,
        is_singleton: bool = True,  # noqa: ARG002
    ) -> None:
        self.lock_file = lock_file
        self.timeout = timeout
        self.blocking = blocking
        self._transaction_lock = threading.Lock()  # serializes the (possibly blocking) SQLite transaction work
        self._internal_lock = threading.Lock()  # protects _lock_level / _current_mode updates and rollback
        self._lock_level = 0
        self._current_mode: Literal["read", "write"] | None = None
        self._write_thread_id: int | None = None
        self.con = sqlite3.connect(self.lock_file, check_same_thread=False)

    def _acquire_transaction_lock(self, *, blocking: bool, timeout: float) -> None:
        if timeout == -1:
            acquired = self._transaction_lock.acquire(blocking)
        else:
            acquired = self._transaction_lock.acquire(blocking, timeout)
        if not acquired:
            raise Timeout(self.lock_file) from None

    def acquire_read(self, timeout: float = -1, *, blocking: bool = True) -> AcquireReturnProxy:
        """
        Acquire a shared read lock.

        If this instance already holds a read lock, the lock level is incremented (reentrant). Attempting to acquire a
        read lock while holding a write lock raises :class:`RuntimeError` (downgrade not allowed).

        :param timeout: maximum wait time in seconds; ``-1`` means block indefinitely
        :param blocking: if ``False``, raise :class:`~filelock.Timeout` immediately when the lock is unavailable
        :raises RuntimeError: if a write lock is already held on this instance
        :raises Timeout: if the lock cannot be acquired within *timeout* seconds
        :return: a proxy that can be used as a context manager to release the lock
        """
        with self._internal_lock:
            if self._lock_level > 0:
                if self._current_mode != "read":
                    msg = (
                        f"Cannot acquire read lock on {self.lock_file} (lock id: {id(self)}): "
                        "already holding a write lock (downgrade not allowed)"
                    )
                    raise RuntimeError(msg)
                self._lock_level += 1
                return AcquireReturnProxy(lock=self)

        start_time = time.perf_counter()
        self._acquire_transaction_lock(blocking=blocking, timeout=timeout)
        try:
            # Double-check: another thread may have acquired the lock while we waited on _transaction_lock.
            with self._internal_lock:
                if self._lock_level > 0:
                    if self._current_mode != "read":
                        msg = (
                            f"Cannot acquire read lock on {self.lock_file} (lock id: {id(self)}): "
                            "already holding a write lock (downgrade not allowed)"
                        )
                        raise RuntimeError(msg)
                    self._lock_level += 1
                    return AcquireReturnProxy(lock=self)

            waited = time.perf_counter() - start_time
            timeout_ms = timeout_for_sqlite(timeout, blocking=blocking, already_waited=waited)
            self.con.execute(f"PRAGMA busy_timeout={timeout_ms};")
            # Use legacy journal mode (not WAL) because WAL does not block readers when a concurrent EXCLUSIVE
            # write transaction is active, making read-write locking impossible without modifying table data.
            # MEMORY is safe here since no actual writes happen — crashes cannot corrupt the DB.
            # See https://sqlite.org/lang_transaction.html#deferred_immediate_and_exclusive_transactions
            #
            # Set here (not in __init__) because this pragma itself may block on a locked database,
            # so it must run after busy_timeout is configured above.
            self.con.execute("PRAGMA journal_mode=MEMORY;")
            # Recompute remaining timeout after the potentially blocking journal_mode pragma.
            waited = time.perf_counter() - start_time
            timeout_ms_2 = timeout_for_sqlite(timeout, blocking=blocking, already_waited=waited)
            if timeout_ms_2 != timeout_ms:
                self.con.execute(f"PRAGMA busy_timeout={timeout_ms_2};")
            self.con.execute("BEGIN TRANSACTION;")
            # A SELECT is needed to force SQLite to actually acquire the SHARED lock on the database.
            # https://www.sqlite.org/lockingv3.html#transaction_control
            self.con.execute("SELECT name from sqlite_schema LIMIT 1;")

            with self._internal_lock:
                self._current_mode = "read"
                self._lock_level = 1

            return AcquireReturnProxy(lock=self)

        except sqlite3.OperationalError as e:
            if "database is locked" not in str(e):
                raise
            raise Timeout(self.lock_file) from None
        finally:
            self._transaction_lock.release()

    def acquire_write(self, timeout: float = -1, *, blocking: bool = True) -> AcquireReturnProxy:
        """
        Acquire an exclusive write lock.

        If this instance already holds a write lock from the same thread, the lock level is incremented (reentrant).
        Attempting to acquire a write lock while holding a read lock raises :class:`RuntimeError` (upgrade not allowed).
        Write locks are pinned to the acquiring thread: a different thread trying to re-enter also raises
        :class:`RuntimeError`.

        :param timeout: maximum wait time in seconds; ``-1`` means block indefinitely
        :param blocking: if ``False``, raise :class:`~filelock.Timeout` immediately when the lock is unavailable
        :raises RuntimeError: if a read lock is already held, or a write lock is held by a different thread
        :raises Timeout: if the lock cannot be acquired within *timeout* seconds
        :return: a proxy that can be used as a context manager to release the lock
        """
        with self._internal_lock:
            if self._lock_level > 0:
                if self._current_mode != "write":
                    msg = (
                        f"Cannot acquire write lock on {self.lock_file} (lock id: {id(self)}): "
                        "already holding a read lock (upgrade not allowed)"
                    )
                    raise RuntimeError(msg)
                cur_thread_id = threading.get_ident()
                if self._write_thread_id != cur_thread_id:
                    msg = (
                        f"Cannot acquire write lock on {self.lock_file} (lock id: {id(self)}) "
                        f"from thread {cur_thread_id} while it is held by thread {self._write_thread_id}"
                    )
                    raise RuntimeError(msg)
                self._lock_level += 1
                return AcquireReturnProxy(lock=self)

        start_time = time.perf_counter()
        self._acquire_transaction_lock(blocking=blocking, timeout=timeout)
        try:
            # Double-check: another thread may have acquired the lock while we waited on _transaction_lock.
            with self._internal_lock:
                if self._lock_level > 0:
                    if self._current_mode != "write":
                        msg = (
                            f"Cannot acquire write lock on {self.lock_file} (lock id: {id(self)}): "
                            "already holding a read lock (upgrade not allowed)"
                        )
                        raise RuntimeError(msg)
                    self._lock_level += 1
                    return AcquireReturnProxy(lock=self)

            waited = time.perf_counter() - start_time
            timeout_ms = timeout_for_sqlite(timeout, blocking=blocking, already_waited=waited)
            self.con.execute(f"PRAGMA busy_timeout={timeout_ms};")
            # See acquire_read() for why journal_mode=MEMORY and why this pragma is set here.
            self.con.execute("PRAGMA journal_mode=MEMORY;")
            # Recompute remaining timeout after the potentially blocking journal_mode pragma.
            waited = time.perf_counter() - start_time
            timeout_ms_2 = timeout_for_sqlite(timeout, blocking=blocking, already_waited=waited)
            if timeout_ms_2 != timeout_ms:
                self.con.execute(f"PRAGMA busy_timeout={timeout_ms_2};")
            self.con.execute("BEGIN EXCLUSIVE TRANSACTION;")

            with self._internal_lock:
                self._current_mode = "write"
                self._lock_level = 1
                self._write_thread_id = threading.get_ident()

            return AcquireReturnProxy(lock=self)

        except sqlite3.OperationalError as e:
            if "database is locked" not in str(e):
                raise
            raise Timeout(self.lock_file) from None
        finally:
            self._transaction_lock.release()

    def release(self, *, force: bool = False) -> None:
        """
        Release one level of the current lock.

        When the lock level reaches zero the underlying SQLite transaction is rolled back, releasing the database lock.

        :param force: if ``True``, release the lock completely regardless of the current lock level
        :raises RuntimeError: if no lock is currently held and *force* is ``False``
        """
        should_rollback = False
        with self._internal_lock:
            if self._lock_level == 0:
                if force:
                    return
                msg = f"Cannot release a lock on {self.lock_file} (lock id: {id(self)}) that is not held"
                raise RuntimeError(msg)
            if force:
                self._lock_level = 0
            else:
                self._lock_level -= 1
            if self._lock_level == 0:
                self._current_mode = None
                self._write_thread_id = None
                should_rollback = True
        if should_rollback:
            self.con.rollback()

    @contextmanager
    def read_lock(self, timeout: float | None = None, *, blocking: bool | None = None) -> Generator[None]:
        """
        Context manager that acquires and releases a shared read lock.

        Falls back to instance defaults for *timeout* and *blocking* when ``None``.

        :param timeout: maximum wait time in seconds, or ``None`` to use the instance default
        :param blocking: if ``False``, raise :class:`~filelock.Timeout` immediately; ``None`` uses the instance default
        """
        if timeout is None:
            timeout = self.timeout
        if blocking is None:
            blocking = self.blocking
        self.acquire_read(timeout, blocking=blocking)
        try:
            yield
        finally:
            self.release()

    @contextmanager
    def write_lock(self, timeout: float | None = None, *, blocking: bool | None = None) -> Generator[None]:
        """
        Context manager that acquires and releases an exclusive write lock.

        Falls back to instance defaults for *timeout* and *blocking* when ``None``.

        :param timeout: maximum wait time in seconds, or ``None`` to use the instance default
        :param blocking: if ``False``, raise :class:`~filelock.Timeout` immediately; ``None`` uses the instance default
        """
        if timeout is None:
            timeout = self.timeout
        if blocking is None:
            blocking = self.blocking
        self.acquire_write(timeout, blocking=blocking)
        try:
            yield
        finally:
            self.release()

    def __del__(self) -> None:
        self.release(force=True)
        self.con.close()
