import os
import sqlite3
import threading
import logging
import time
from _error import Timeout
from filelock._api import AcquireReturnProxy, BaseFileLock
from typing import Literal, Any
from contextlib import contextmanager
from weakref import WeakValueDictionary

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
        raise ValueError("timeout must be a non-negative number or -1")
    
    if timeout > 0:
        timeout = timeout - already_waited
        if timeout < 0:
            timeout = 0
    
    assert timeout >= 0

    timeout_ms = int(timeout * 1000)
    if timeout_ms > _MAX_SQLITE_TIMEOUT_MS or timeout_ms < 0:
        _LOGGER.warning("timeout %s is too large for SQLite, using %s ms instead", timeout, _MAX_SQLITE_TIMEOUT_MS)
        return _MAX_SQLITE_TIMEOUT_MS
    return timeout_ms


class _ReadWriteLockMeta(type):
    """Metaclass that redirects instance creation to get_lock() when is_singleton=True."""
    def __call__(cls, lock_file: str | os.PathLike[str], 
                 timeout: float = -1, blocking: bool = True, 
                 is_singleton: bool = True, *args: Any, **kwargs: Any) -> "ReadWriteLock":
        if is_singleton:
            return cls.get_lock(lock_file, timeout, blocking)
        return super().__call__(lock_file, timeout, blocking, is_singleton, *args, **kwargs)


class ReadWriteLock(metaclass=_ReadWriteLockMeta):
    # Singleton storage and its lock.
    _instances = WeakValueDictionary()
    _instances_lock = threading.Lock()

    @classmethod
    def get_lock(cls, lock_file: str | os.PathLike[str],
                 timeout: float = -1, blocking: bool = True) -> "ReadWriteLock":
        """Return the one-and-only ReadWriteLock for a given file."""
        normalized = os.path.abspath(lock_file)
        with cls._instances_lock:
            if normalized not in cls._instances:
                cls._instances[normalized] = cls(lock_file, timeout, blocking)
            instance = cls._instances[normalized]
            if instance.timeout != timeout or instance.blocking != blocking:
                raise ValueError("Singleton lock created with timeout=%s, blocking=%s, cannot be changed to timeout=%s, blocking=%s", instance.timeout, instance.blocking, timeout, blocking)
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
        self._current_mode: Literal["read", "write", None] = None
        # _lock_level is the reentrance counter.
        self._lock_level = 0
        self.con = sqlite3.connect(self.lock_file, check_same_thread=False)
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
        self.con.execute('PRAGMA journal_mode=MEMORY;')

    def acquire_read(self, timeout: float = -1, blocking: bool = True) -> AcquireReturnProxy:
        """Acquire a read lock. If a lock is already held, it must be a read lock.
        Upgrading from read to write is prohibited."""

        # Attempt to re-enter already held lock.
        with self._internal_lock:
            if self._lock_level > 0:
                # Must already be in read mode.
                if self._current_mode != "read":
                        raise RuntimeError(
                            f"Cannot acquire read lock on {self.lock_file} (lock id: {id(self)}): "
                            "already holding a write lock (downgrade not allowed)"
                        )
                self._lock_level += 1
                return AcquireReturnProxy(lock=self)

        timeout_ms = timeout_for_sqlite(timeout, blocking)

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
                        raise RuntimeError(
                            f"Cannot acquire read lock on {self.lock_file} (lock id: {id(self)}): "
                            "already holding a write lock (downgrade not allowed)"
                        )
                    self._lock_level += 1
                    return AcquireReturnProxy(lock=self)
                
            waited = time.perf_counter() - start_time
            timeout_ms = timeout_for_sqlite(timeout, blocking, waited)
            
            self.con.execute('PRAGMA busy_timeout=?;', (timeout_ms,))
            self.con.execute('BEGIN TRANSACTION;')
            # Need to make SELECT to compel SQLite to actually acquire a SHARED db lock.
            # See https://www.sqlite.org/lockingv3.html#transaction_control
            self.con.execute('SELECT name from sqlite_schema LIMIT 1;')

            with self._internal_lock:
                self._current_mode = "read"
                self._lock_level = 1
            
            return AcquireReturnProxy(lock=self)

        except sqlite3.OperationalError as e:
            if 'database is locked' not in str(e):
                raise  # Re-raise unexpected errors.
            raise Timeout(self.lock_file)
        finally:
            self._transaction_lock.release()

    def acquire_write(self, timeout: float = -1, blocking: bool = True) -> AcquireReturnProxy:
        """Acquire a write lock. If a lock is already held, it must be a write lock.
        Upgrading from read to write is prohibited."""

        # Attempt to re-enter already held lock.
        with self._internal_lock:
            if self._lock_level > 0:
                if self._current_mode != "write":
                    raise RuntimeError(
                        f"Cannot acquire write lock on {self.lock_file} (lock id: {id(self)}): "
                        "already holding a read lock (upgrade not allowed)"
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
                        raise RuntimeError(
                            f"Cannot acquire write lock on {self.lock_file} (lock id: {id(self)}): "
                            "already holding a read lock (upgrade not allowed)"
                        )
                    self._lock_level += 1
                    return AcquireReturnProxy(lock=self)
                
            waited = time.perf_counter() - start_time
            timeout_ms = timeout_for_sqlite(timeout, blocking, waited)
                
            self.con.execute('PRAGMA busy_timeout=?;', (timeout_ms,))
            self.con.execute('BEGIN EXCLUSIVE TRANSACTION;')

            with self._internal_lock:
                self._current_mode = "write"
                self._lock_level = 1
            
            return AcquireReturnProxy(lock=self)

        except sqlite3.OperationalError as e:
            if 'database is locked' not in str(e):
                raise  # Re-raise if it is an unexpected error.
            raise Timeout(self.lock_file)
        finally:
            self._transaction_lock.release()

    def release(self, force: bool = False) -> None:
        with self._internal_lock:
            if self._lock_level == 0:
                if force:
                    return
                raise RuntimeError(f"Cannot release a lock on {self.lock_file} (lock id: {id(self)}) that is not held")
            if force:
                self._lock_level = 0
            else:
                self._lock_level -= 1
            if self._lock_level == 0:
                # Clear current mode and rollback the SQLite transaction.
                self._current_mode = None
                # Unless there are bugs in this code, sqlite3.ProgrammingError
                # must not be raise here, that is, the transaction should have been
                # started in acquire().
                self.con.rollback()

    # ----- Context Manager Protocol -----
    # (We provide two context managers as helpers.)

    @contextmanager
    def read_lock(self, timeout: float | None = None,
                  blocking: bool | None = None):
        """Context manager for acquiring a read lock.
        Attempts to upgrade to write lock are disallowed."""
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
    def write_lock(self, timeout: float | None = None,
                   blocking: bool | None = None):
        """Context manager for acquiring a write lock.
        Acquiring read locks on the same file while helding a write lock is prohibited."""
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


