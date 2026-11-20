Bound parent-directory enumeration in the dead-holder link sweep during :class:`~filelock.ReadWriteLock` and
:class:`~filelock.AsyncReadWriteLock` connection setup. A crowded shared directory no longer forces the sweep to inspect
every planted name.
