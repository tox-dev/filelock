:func:`~filelock.lock_descriptor` now raises :class:`TypeError` for a *blocking* that is not a :class:`bool`, as
:class:`~filelock.FileLock` and :class:`~filelock.ReadWriteLock` already do. Before, ``blocking=0`` was treated as
non-blocking and truthy non-bool values such as ``"false"`` entered the blocking retry loop.
