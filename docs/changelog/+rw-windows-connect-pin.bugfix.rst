On Windows, :class:`~filelock.ReadWriteLock` rejects junctions in its private database alias path and prevents
directory renames until SQLite opens the database.
