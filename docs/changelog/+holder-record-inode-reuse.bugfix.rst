A :class:`~filelock.SoftReadWriteLock` heartbeat checks the holder token before writing. If another file replaces the
record and reuses its inode, the lock reports a lost hold.
