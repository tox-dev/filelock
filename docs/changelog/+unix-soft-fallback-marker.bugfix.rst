On filesystems without ``flock``, :class:`~filelock.UnixFileLock` publishes the soft marker through its open descriptor.
A file writable by other accounts requires an atomic exchange for a fresh marker with restricted permissions.
