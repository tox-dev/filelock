On macOS and systems with ``CLOCK_BOOTTIME``, :class:`~filelock.SoftReadWriteLock` includes system sleep when checking
waiter leases and renews expired tokens before entry.
