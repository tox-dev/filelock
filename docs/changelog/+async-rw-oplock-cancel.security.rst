Canceling an :class:`~filelock.AsyncReadWriteLock` acquire on Windows now stops its wait for a database oplock, even when
the lock's own timeout is longer than the caller's deadline.
