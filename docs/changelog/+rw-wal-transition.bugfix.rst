``ReadWriteLock`` and ``AsyncReadWriteLock`` refuse an acquisition when SQLite enters WAL mode after the database's
initial header check.
