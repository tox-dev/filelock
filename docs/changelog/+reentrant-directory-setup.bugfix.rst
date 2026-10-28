A nested acquire of a held :class:`~filelock.FileLock` or :class:`~filelock.SoftFileLock` no longer creates the lock
file's parent directory. After a rename of that directory during the hold, the nested acquire recreated the old name,
and when a file had taken the name it raised :class:`FileExistsError` and left the hold's recursion count incremented.
