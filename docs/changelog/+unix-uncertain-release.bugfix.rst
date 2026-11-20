On Unix, :class:`~filelock.FileLock` refuses reentrant acquisition after an ambiguous unlock error until an explicit
``release()`` retry resolves the hold. A signal handler that raised after the kernel unlocked could previously let the
same instance enter without a lock.
