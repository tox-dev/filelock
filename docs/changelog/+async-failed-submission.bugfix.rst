:class:`~filelock.AsyncFileLock`, :class:`~filelock.AsyncReadWriteLock` and :class:`~filelock.AsyncSoftReadWriteLock`
no longer run a backend call after its submission to the executor raised. A thread pool that cannot start a worker
thread raises ``RuntimeError: can't start new thread`` but keeps the call queued, so a release that had failed this way
ran later and dropped the lock under the next task that entered it.
