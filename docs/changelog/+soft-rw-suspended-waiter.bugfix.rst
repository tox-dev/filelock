On systems with ``CLOCK_BOOTTIME``, a :class:`~filelock.SoftReadWriteLock` waiter that resumes after host suspension
starts under a new token if a peer could have judged its old record stale. This prevents a delayed sweep from removing
the record of a newly granted holder.
