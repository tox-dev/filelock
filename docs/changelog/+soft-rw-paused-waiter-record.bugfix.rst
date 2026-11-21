A :class:`~filelock.SoftReadWriteLock` waiter paused past ``stale_threshold`` gets a fresh token so cleanup of its old
record cannot remove the new hold.
