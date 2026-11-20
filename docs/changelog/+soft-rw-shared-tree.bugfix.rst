:class:`~filelock.SoftReadWriteLock` under a sticky parent such as ``/tmp`` now refuses a ``<path>.rw`` tree when that
tree, ``gen`` or ``holders`` is writable by other accounts. It also checks the owner of ``gen`` and ``holders`` against
the lock's parent directory on every open, including after a directory's inode number is reused. An account could move
a group-writable tree owned by the lock's account into place or reuse a removed tree's inode number, then publish
snapshots that admitted two writers. Remove an unsafe existing tree so the lock can create private directories.
