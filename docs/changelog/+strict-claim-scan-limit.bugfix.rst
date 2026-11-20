Acquisition with :class:`~filelock.StrictSoftFileLock` backs off when it finds more than 1024 public claims.
A directory with more than 16,380 entries before publication raises :class:`~filelock.SoftFileLockProtocolError`.
This leaves four slots for publication within the 16,384-entry scan limit. Sentinel cleanup counts only records
belonging to the lock toward its limit on records checked.
