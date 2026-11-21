On Windows, :class:`~filelock.SoftFileLock` rechecks its acquire deadline after each half-second attempt to rename a
stale marker held by a reader's oplock.
