On Windows, :class:`~filelock.StrictSoftFileLock` honors its acquire timeout when an oplock blocks the sentinel. On
Linux, a sentinel lease counts as contention after a short grace period, including for ``blocking=False``.
