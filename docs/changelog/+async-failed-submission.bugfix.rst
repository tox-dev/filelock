:class:`~filelock.AsyncFileLock`, :class:`~filelock.AsyncReadWriteLock` and :class:`~filelock.AsyncSoftReadWriteLock`
discard executor calls whose submission failed. A queued release can no longer drop a later task's lock.
