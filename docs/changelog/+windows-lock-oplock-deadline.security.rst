On Windows, ``FileLock`` treats a pending oplock break as contention and retries up to the caller's deadline.
``acquire(blocking=False)`` now raises ``Timeout`` immediately, even if the oplock holder would have responded within the
previous half-second grace period.
