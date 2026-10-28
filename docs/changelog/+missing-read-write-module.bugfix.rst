Resolving :class:`~filelock.ReadWriteLock` or :class:`~filelock.AsyncReadWriteLock` raises the :exc:`ImportError` when a
module other than Python's :mod:`sqlite3` is missing, as in a frozen application that left out a filelock submodule. The
names are ``None`` only without SQLite.
