On Unix, :class:`~filelock.ReadWriteLock` raises :class:`PermissionError` in a sticky directory such as ``/tmp``
unless the caller or the directory owner owns the lock database. Another owner could replace the database during a hold
and split readers and writers across two databases.
