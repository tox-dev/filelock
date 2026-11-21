In a sticky Unix directory such as ``/tmp``, :class:`~filelock.ReadWriteLock` raises :class:`PermissionError` unless
the caller or directory owner owns the database.
