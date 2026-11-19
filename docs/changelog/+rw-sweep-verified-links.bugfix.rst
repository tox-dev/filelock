On POSIX, :class:`~filelock.ReadWriteLock` and :class:`~filelock.AsyncReadWriteLock` cleanup no longer remove empty
directories merely because they carry a dead holder's name. A sweep attempts directory removal only after unlinking a
verified hard link to the current lock database.
