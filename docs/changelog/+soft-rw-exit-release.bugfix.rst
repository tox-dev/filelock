At interpreter exit, :class:`~filelock.SoftReadWriteLock` keeps holds owned by live daemon threads.
:class:`~filelock.AsyncSoftReadWriteLock` keeps holds whose event loops are still running.
