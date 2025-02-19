import os
import sqlite3
import threading

from _error import Timeout
from filelock._api import BaseFileLock

class _SQLiteLock(BaseFileLock):
    def __init__(self, lock_file: str | os.PathLike[str], timeout: float = -1, blocking: bool = True):
        super().__init__(lock_file, timeout, blocking)
        self.procLock = threading.Lock()
        self.con = sqlite3.connect(self._context.lock_file, check_same_thread=False)
        # Redundant unless there are "rogue" processes that open the db
        # and switch the the db to journal_mode=WAL.
        # Using the legacy journal mode rather than more modern WAL mode because,
        # apparently, in WAL mode it's impossible to enforce that read transactions
        # (started with BEGIN TRANSACTION) are blocked if a concurrent write transaction,
        # even EXCLUSIVE, is in progress, unless the read transactions actually read
        # any pages modified by the write transaction. But in the legacy journal mode,
        # it seems, it's possible to do this read-write locking without table data
        # modification at each exclusive lock.
        # See https://sqlite.org/lang_transaction.html#deferred_immediate_and_exclusive_transactions
        self.con.execute('PRAGMA journal_mode=DELETE;')
        self.cur = None
    
    def _release(self):
        with self.procLock:
            if self.cur is None:
                return  # Nothing to release
            try:
                self.cur.execute('ROLLBACK TRANSACTION;')
            except sqlite3.ProgrammingError:
                pass  # Already rolled back or transaction not active
            finally:
                self.cur.close()
                self.cur = None

class WriteLock(_SQLiteLock):
    def _acquire(self) -> None:
        timeout_ms = int(self._context.timeout*1000) if self._context.blocking else 0
        with self.procLock:
            if self.cur is not None:
                return
            self.con.execute('PRAGMA busy_timeout=?;', (timeout_ms,))
            try:
                self.cur = self.con.execute('BEGIN EXCLUSIVE TRANSACTION;')
            except sqlite3.OperationalError as e:
                if 'database is locked' not in str(e):
                    raise  # Re-raise unexpected errors
                raise Timeout(self._context.lock_file)

class ReadLock(_SQLiteLock):
    def _acquire(self):
        timeout_ms = int(self._context.timeout * 1000) if self._context.blocking else 0
        with self.procLock:
            if self.cur is not None:
                return
            self.con.execute('PRAGMA busy_timeout=?;', (timeout_ms,))
            cur = None  # Initialize cur to avoid potential UnboundLocalError
            try:
                cur = self.con.execute('BEGIN TRANSACTION;')
                # BEGIN doesn't itself acquire a SHARED lock on the db, that is needed for
                # effective exclusion with writeLock(). A SELECT is needed.
                cur.execute('SELECT name from sqlite_schema LIMIT 1;')
                self.cur = cur
            except sqlite3.OperationalError as e:
                if 'database is locked' not in str(e):
                    raise  # Re-raise unexpected errors
                if cur is not None:
                    cur.close()
                raise Timeout(self._context.lock_file)



