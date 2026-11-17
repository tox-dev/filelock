:class:`~filelock.AsyncStrictSoftFileLock` with a ``poll_interval`` below 0.05 seconds now spreads contended
retries across a jittered, widening window like :class:`~filelock.StrictSoftFileLock`, instead of a fixed interval.
