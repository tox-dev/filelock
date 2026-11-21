On POSIX, :class:`~filelock.ReadWriteLock` and :class:`~filelock.AsyncReadWriteLock` preserve mutual exclusion if
their private link disappears before SQLite opens it. Linux cleanup preserves peer links when procfs cannot identify
their PID namespace.
