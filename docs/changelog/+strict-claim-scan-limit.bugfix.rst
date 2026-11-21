:class:`~filelock.StrictSoftFileLock` backs off above 1,024 public claims and raises
:class:`~filelock.SoftFileLockProtocolError` above 16,380 directory entries before publication. Sentinel cleanup
counts only the lock's own records.
