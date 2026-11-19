:class:`~filelock.ReadWriteLock` refuses, with ``PermissionError``, a lock database that another account owns under a
sticky parent such as ``/tmp``, as :class:`~filelock.UnixFileLock` does for its lock file. Its owner could remove it
during a hold, after which a second process opened a new database at the same path and both held the lock.
