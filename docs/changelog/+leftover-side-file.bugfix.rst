On POSIX and Windows, :class:`~filelock.ReadWriteLock` opens its database through a private hard link if a
``-journal``, ``-wal`` or ``-shm`` file already exists, preventing replay of a leftover journal.
