Fix a race in :class:`~filelock.SoftFileLock`, :class:`~filelock.MarkerSoftFileLock` and
:class:`~filelock.SoftFileLease` when three or more contenders reclaim a dead marker. The race could strand a live
holder's marker under a break name, leaving the lock unusable or admitting a second holder. Contenders now recheck the
marker before exchanging names and unwind nested exchanges in order. If the filesystem refuses a return exchange, the
contender leaves its marker at the lock path for later recovery.
