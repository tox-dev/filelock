On Windows, lock paths that differ only in drive letter or UNC share case, such as ``C:\work.lock`` and
``c:\work.lock``, now share one :class:`~filelock.FileLock` or :class:`~filelock.SoftReadWriteLock` singleton and
trigger ``FileLock``'s same-thread deadlock detection.
