On POSIX systems with atomic name exchange, :class:`~filelock.SoftFileLock` waits for a live stale-lock breaker before
recovering its marker.
