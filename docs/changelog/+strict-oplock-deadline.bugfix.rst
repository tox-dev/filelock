A read-handle oplock on a :class:`~filelock.StrictSoftFileLock` sentinel no longer holds ``acquire()`` past its timeout on
Windows. A Linux file lease on the sentinel also counts as contention after a short grace period. Calls with
``blocking=False`` still allow that grace period before raising :class:`~filelock.Timeout`.
