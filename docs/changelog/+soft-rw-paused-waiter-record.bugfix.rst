A :class:`~filelock.SoftReadWriteLock` contender that pauses past its ``stale_threshold`` while waiting now starts
under a new token. A delayed sweep of its old record can no longer remove the record of a newly granted holder.
