On macOS and systems with ``CLOCK_BOOTTIME``, :class:`~filelock.SoftReadWriteLock` counts time spent asleep when it
checks the age of a waiter's token. A waiter that slept past its lease requests a new token before entering.
