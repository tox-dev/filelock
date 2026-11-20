On POSIX, :class:`~filelock.ReadWriteLock` and :class:`~filelock.AsyncReadWriteLock` remove empty link directories left
by dead holders. Cleanup removes a subdirectory only after unlinking a verified hard link to the current lock database
inside it.
