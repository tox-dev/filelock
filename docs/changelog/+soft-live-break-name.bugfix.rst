On POSIX systems with atomic name exchange, :class:`~filelock.SoftFileLock` waits when a stale marker points to a break
name whose PID still runs. This prevents recovery from moving an active breaker's marker and admitting another holder.
