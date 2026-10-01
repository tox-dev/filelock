``ReadWriteLock`` and ``AsyncReadWriteLock`` refuse a symlink at the database path instead of following it, so a
user who can create names in a shared lock directory cannot point the lock at another file (GHSA-j8f7-rjxc-mr56).
