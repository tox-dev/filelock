On Windows, :class:`~filelock.SoftFileLock` refuses a dangling symlink at the lock path instead of creating the link's
target and leaving a marker record there.
