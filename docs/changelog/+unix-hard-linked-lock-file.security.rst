On Unix, :class:`~filelock.FileLock` raises :class:`PermissionError` when the lock file has another hard link. Where
the kernel lets an account link a file it does not own, such as macOS, another user could link one of the caller's
files to the lock path, and an ``on_acquired`` hook would then write or truncate that file.
