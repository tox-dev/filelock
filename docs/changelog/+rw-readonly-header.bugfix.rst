``ReadWriteLock`` and ``AsyncReadWriteLock`` refuse SQLite lock databases with unsupported file-format versions,
including a write version that makes SQLite treat an exclusive transaction as a read transaction.
