On Unix, acquiring a lock with an ``on_acquired`` callback raises :class:`PermissionError` when the lock file has
another hard link and another account can write its directory. Without a callback, or in a directory only the caller
can write, hard-linked lock names still share the same native lock.
