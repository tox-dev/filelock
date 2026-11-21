Under a sticky parent, :class:`~filelock.SoftReadWriteLock` rejects a ``<path>.rw`` tree, ``gen`` or ``holders``
writable by other accounts and rechecks subdirectory ownership on open. Remove unsafe trees so the lock can recreate
them privately.
