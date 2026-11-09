Contended :class:`~filelock.FileLock` and :class:`~filelock.SoftFileLock` acquisitions now raise
:class:`~filelock.Timeout` when elapsed time reaches the requested timeout. A clock that landed exactly on the
deadline previously left a waiter retrying with zero-length sleeps indefinitely.
