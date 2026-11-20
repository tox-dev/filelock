On POSIX, :class:`~filelock.ReadWriteLock` and :class:`~filelock.AsyncReadWriteLock` no longer create a separate lock
database when a private hard link disappears before SQLite opens it. On Linux, cleanup leaves links from other
processes alone when procfs cannot establish their PID namespace.
