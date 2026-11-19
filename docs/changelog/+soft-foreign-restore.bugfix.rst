On POSIX systems with atomic name exchange, :class:`~filelock.SoftFileLock` no longer puts another account's recovery
record at the lock path under a sticky parent. Its owner could remove that record during recovery and leave a live
holder's marker stranded.
