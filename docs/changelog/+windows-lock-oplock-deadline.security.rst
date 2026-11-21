On Windows, ``FileLock`` treats a pending oplock break as contention until the acquire deadline.
``acquire(blocking=False)`` raises ``Timeout`` immediately.
