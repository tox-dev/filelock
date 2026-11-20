On Windows, :class:`~filelock.ReadWriteLock` rejects junctions in the private database alias path and prevents the alias
directories from being renamed until SQLite opens the database.
