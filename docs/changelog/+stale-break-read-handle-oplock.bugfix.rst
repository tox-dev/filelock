On Windows, a read-handle oplock on a stale :class:`~filelock.SoftFileLock` marker no longer leaves ``acquire()``
waiting indefinitely for its holder. The open before the rename does not break this oplock. The rename's wait is now
canceled after half a second per attempt, then the acquire checks its deadline.
