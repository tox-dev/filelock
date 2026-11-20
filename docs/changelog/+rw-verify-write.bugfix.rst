``ReadWriteLock`` and ``AsyncReadWriteLock`` refuse a write lock when SQLite opens its connection read-only, even if
the initial file header allowed writes.
