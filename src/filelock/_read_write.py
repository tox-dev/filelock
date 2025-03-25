from __future__ import annotations

import logging
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from typing import Any, Literal
from weakref import WeakValueDictionary

from filelock._api import AcquireReturnProxy

from ._error import Timeout

_LOGGER = logging.getLogger("filelock")

# PRAGMA busy_timeout=N delegates to https://www.sqlite.org/c3ref/busy_timeout.html,
# which accepts an int argument, which has the maximum value of 2_147_483_647 on 32-bit
# systems. Use even a lower value to be safe. This 2 bln milliseconds is about 23 days.
_MAX_SQLITE_TIMEOUT_MS = 2_000_000_000 - 1


def timeout_for_sqlite(timeout: float, blocking: bool, already_waited: float) -> int:
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

    assert timeout >= 0

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
        blocking: bool = True,
        is_singleton: bool = True,
        *args: Any,
        **kwargs: Any,
    ) -> ReadWriteLock:
        if is_singleton:
            return cls.get_lock(lock_file, timeout, blocking)
        return super().__call__(lock_file, timeout, blocking, is_singleton, *args, **kwargs)


class ReadWriteLock(metaclass=_ReadWriteLockMeta):
    # Singleton storage and its lock.
    _instances = WeakValueDictionary()
    _instances_lock = threading.Lock()

    @classmethod
    def get_lock(cls, lock_file: str | os.PathLike[str], timeout: float = -1, blocking: bool = True) -> ReadWriteLock:
        """Return the one-and-only ReadWriteLock for a given file."""
        normalized = os.path.abspath(lock_file)
        with cls._instances_lock:
            if normalized not in cls._instances:
                # Create the instance with a strong reference first
                instance = super(_ReadWriteLockMeta, cls).__call__(lock_file, timeout, blocking, is_singleton=False)
                cls._instances[normalized] = instance
            else:
                instance = cls._instances[normalized]

            if instance.timeout != timeout or instance.blocking != blocking:
                msg = (
                    "Singleton lock created with timeout=%s, blocking=%s, cannot be changed to timeout=%s, blocking=%s"
                )
                raise ValueError(
                    msg,
                    instance.timeout,
                    instance.blocking,
                    timeout,
                    blocking,
                )
            return instance

    def __init__(
        self,
        lock_file: str | os.PathLike[str],
        timeout: float = -1,
        blocking: bool = True,
        is_singleton: bool = True,
    ) -> None:
        self.lock_file = lock_file
        self.timeout = timeout
        self.blocking = blocking
        # _transaction_lock is for the SQLite transaction work.
        self._transaction_lock = threading.Lock()
        # _internal_lock protects the short critical sections that update _lock_level
        # and rollback the transaction in release().
        self._internal_lock = threading.Lock()
        self._lock_level = 0  # Reentrance counter.
        # _current_mode holds the active lock mode ("read" or "write") or None if no lock is held.
        self._current_mode: Literal["read", "write"] | None = None
        # _lock_level is the reentrance counter.
        self._lock_level = 0
        self._write_thread_id: int | None = None
        self.con = sqlite3.connect(self.lock_file, check_same_thread=False)

    def acquire_read(self, timeout: float = -1, blocking: bool = True) -> AcquireReturnProxy:
        """
        Acquire a read lock. If a lock is already held, it must be a read lock.
        Upgrading from read to write is prohibited.
        """
        # Attempt to re-enter already held lock.
        with self._internal_lock:
            if self._lock_level > 0:
                # Must already be in read mode.
                if self._current_mode != "read":
                    msg = (
                        f"Cannot acquire read lock on {self.lock_file} (lock id: {id(self)}): "
                        "already holding a write lock (downgrade not allowed)"
                    )
                    raise RuntimeError(
                        msg
                    )
                self._lock_level += 1
                return AcquireReturnProxy(lock=self)

        start_time = time.perf_counter()
        # Acquire the transaction lock so that the (possibly blocking) SQLite work
        # happens without conflicting with other threads' transaction work.
        if not self._transaction_lock.acquire(blocking, timeout):
            raise Timeout(self.lock_file)
        try:
            # Double-check: another thread might have completed acquisition meanwhile.
            with self._internal_lock:
                if self._lock_level > 0:
                    if self._current_mode != "read":
                        msg = (
                            f"Cannot acquire read lock on {self.lock_file} (lock id: {id(self)}): "
                            "already holding a write lock (downgrade not allowed)"
                        )
                        raise RuntimeError(
                            msg
                        )
                    self._lock_level += 1
                    return AcquireReturnProxy(lock=self)

            waited = time.perf_counter() - start_time
            timeout_ms = timeout_for_sqlite(timeout, blocking, waited)
            self.con.execute("PRAGMA busy_timeout=%d;" % timeout_ms)
            # WHY journal_mode=MEMORY?
            # Using the legacy journal mode rather than more modern WAL mode because,
            # apparently, in WAL mode it's impossible to enforce that read transactions
            # (started with BEGIN TRANSACTION) are blocked if a concurrent write transaction,
            # even EXCLUSIVE, is in progress, unless the read transactions actually read
            # any pages modified by the write transaction. But in the legacy journal mode,
            # it seems, it's possible to do this read-write locking without table data
            # modification at each exclusive lock.
            # See https://sqlite.org/lang_transaction.html#deferred_immediate_and_exclusive_transactions
            # "MEMORY" journal mode is fine because no actual writes to the are happening in write-lock
            # acquire, so crashes cannot adversely affect the DB. Even journal_mode=OFF would probably
            # be fine, too, but the SQLite documentation says that ROLLBACK becomes *undefined behaviour*
            # with journal_mode=OFF which sounds scarier.
            #
            # WHY SETTING THIS PRAGMA HERE RATHER THAN IN ReadWriteLock.__init__()?
            # Because setting this pragma may block on the database if it is locked at the moment,
            # so we must set this pragma *after* `PRAGMA busy_timeout` above.
            self.con.execute("PRAGMA journal_mode=MEMORY;")
            # Recompute the remaining timeout after the potentially blocking pragma
            # statement above.
            waited = time.perf_counter() - start_time
            timeout_ms_2 = timeout_for_sqlite(timeout, blocking, waited)
            if timeout_ms_2 != timeout_ms:
                self.con.execute("PRAGMA busy_timeout=%d;" % timeout_ms_2)
            self.con.execute("BEGIN TRANSACTION;")
            # Need to make SELECT to compel SQLite to actually acquire a SHARED db lock.
            # See https://www.sqlite.org/lockingv3.html#transaction_control
            self.con.execute("SELECT name from sqlite_schema LIMIT 1;")

            with self._internal_lock:
                self._current_mode = "read"
                self._lock_level = 1

            return AcquireReturnProxy(lock=self)

        except sqlite3.OperationalError as e:
            if "database is locked" not in str(e):
                raise  # Re-raise unexpected errors.
            raise Timeout(self.lock_file)
        finally:
            self._transaction_lock.release()

    def acquire_write(self, timeout: float = -1, blocking: bool = True) -> AcquireReturnProxy:
        """
        Acquire a write lock. If a lock is already held, it must be a write lock.
        Upgrading from read to write is prohibited.
        """
        # Attempt to re-enter already held lock.
        with self._internal_lock:
            if self._lock_level > 0:
                if self._current_mode != "write":
                    msg = (
                        f"Cannot acquire write lock on {self.lock_file} (lock id: {id(self)}): "
                        "already holding a read lock (upgrade not allowed)"
                    )
                    raise RuntimeError(
                        msg
                    )
                cur_thread_id = threading.get_ident()
                if self._write_thread_id != cur_thread_id:
                    msg = (
                        f"Cannot acquire write lock on {self.lock_file} (lock id: {id(self)}) "
                        f"from thread {cur_thread_id} while it is held by thread {self._write_thread_id}"
                    )
                    raise RuntimeError(
                        msg
                    )
                self._lock_level += 1
                return AcquireReturnProxy(lock=self)

        start_time = time.perf_counter()
        # Acquire the transaction lock so that the (possibly blocking) SQLite work
        # happens without conflicting with other threads' transaction work.
        if not self._transaction_lock.acquire(blocking, timeout):
            raise Timeout(self.lock_file)
        try:
            # Double-check: another thread might have completed acquisition meanwhile.
            with self._internal_lock:
                if self._lock_level > 0:
                    if self._current_mode != "write":
                        msg = (
                            f"Cannot acquire write lock on {self.lock_file} (lock id: {id(self)}): "
                            "already holding a read lock (upgrade not allowed)"
                        )
                        raise RuntimeError(
                            msg
                        )
                    self._lock_level += 1
                    return AcquireReturnProxy(lock=self)

            waited = time.perf_counter() - start_time
            timeout_ms = timeout_for_sqlite(timeout, blocking, waited)
            self.con.execute("PRAGMA busy_timeout=%d;" % timeout_ms)
            # For explanations for both why we use journal_mode=MEMORY and why we set
            # this pragma here rather than in ReadWriteLock.__init__(), see the comments
            # in acquire_read().
            self.con.execute("PRAGMA journal_mode=MEMORY;")
            # Recompute the remaining timeout after the potentially blocking pragma
            # statement above.
            waited = time.perf_counter() - start_time
            timeout_ms_2 = timeout_for_sqlite(timeout, blocking, waited)
            if timeout_ms_2 != timeout_ms:
                self.con.execute("PRAGMA busy_timeout=%d;" % timeout_ms_2)
            self.con.execute("BEGIN EXCLUSIVE TRANSACTION;")

            with self._internal_lock:
                self._current_mode = "write"
                self._lock_level = 1
                self._write_thread_id = threading.get_ident()

            return AcquireReturnProxy(lock=self)

        except sqlite3.OperationalError as e:
            if "database is locked" not in str(e):
                raise  # Re-raise unexpected errors.
            raise Timeout(self.lock_file)
        finally:
            self._transaction_lock.release()

    def release(self, force: bool = False) -> None:
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
                # Clear current mode and rollback the SQLite transaction.
                self._current_mode = None
                self._write_thread_id = None
                # Unless there are bugs in this code, sqlite3.ProgrammingError
                # must not be raise here, that is, the transaction should have been
                # started in acquire_read() or acquire_write().
                self.con.rollback()

    # ----- Context Manager Protocol -----
    # (We provide two context managers as helpers.)

    @contextmanager
    def read_lock(self, timeout: float | None = None, blocking: bool | None = None):
        """
        Context manager for acquiring a read lock.
        Attempts to upgrade to write lock are disallowed.
        """
        if timeout is None:
            timeout = self.timeout
        if blocking is None:
            blocking = self.blocking
        self.acquire_read(timeout, blocking)
        try:
            yield
        finally:
            self.release()

    @contextmanager
    def write_lock(self, timeout: float | None = None, blocking: bool | None = None):
        """
        Context manager for acquiring a write lock.
        Acquiring read locks on the same file while helding a write lock is prohibited.
        """
        if timeout is None:
            timeout = self.timeout
        if blocking is None:
            blocking = self.blocking
        self.acquire_write(timeout, blocking)
        try:
            yield
        finally:
            self.release()

    def __del__(self) -> None:
        """Called when the lock object is deleted."""
        self.release(force=True)
