:class:`~filelock.StrictSoftFileLock` now bounds claim reads and abandoned-record cleanup within one acquisition
attempt. A planted claim flood could otherwise keep ``acquire(blocking=False)`` or an acquisition with a short
``timeout`` busy beyond that bound.
