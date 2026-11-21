On POSIX, :class:`~filelock.ReadWriteLock` and :class:`~filelock.AsyncReadWriteLock` remove dead holders' empty link
directories after verifying the link belongs to the current database.
