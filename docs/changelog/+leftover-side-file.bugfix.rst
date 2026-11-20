Keep :class:`~filelock.ReadWriteLock` from replaying a ``-journal`` left beside its database before the directory was
made private. Such a journal could make SQLite delete a file named in it. An existing ``-journal``, ``-wal`` or ``-shm``
now selects the private hard-link route on POSIX and Windows.
