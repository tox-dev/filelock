On Linux, :class:`~filelock.ReadWriteLock` and :class:`~filelock.AsyncReadWriteLock` honor the acquire timeout without
busy-waiting on a leased database. Nonblocking acquires return promptly.
