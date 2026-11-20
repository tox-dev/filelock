On Linux, :class:`~filelock.ReadWriteLock` and :class:`~filelock.AsyncReadWriteLock` now wait between attempts when a
file lease refuses a nonblocking open of the database. Blocking acquires honor their timeout without consuming a CPU
core during the wait. Nonblocking acquires still return promptly.
