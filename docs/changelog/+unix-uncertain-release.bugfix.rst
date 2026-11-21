After an ambiguous Unix unlock error, :class:`~filelock.FileLock` rejects reentrant acquires until an explicit
``release()`` retry resolves the hold.
