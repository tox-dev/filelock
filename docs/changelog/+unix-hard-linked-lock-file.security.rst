On Unix, acquiring a lock with an ``on_acquired`` callback raises :class:`PermissionError` when the lock file has
another hard link. Without a callback, hard-linked lock names still share the same native lock.
