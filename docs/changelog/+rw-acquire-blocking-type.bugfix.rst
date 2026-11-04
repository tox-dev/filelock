:class:`~filelock.ReadWriteLock`, :class:`~filelock.SoftReadWriteLock` and their async wrappers raise
:class:`TypeError` for a *blocking* that is not a :class:`bool`, in the constructor and on acquire, as
:class:`~filelock.FileLock` does since the fix for non-bool *blocking* in ``acquire()``. ``blocking=0`` used to wait for
the full timeout on a contended :class:`~filelock.ReadWriteLock` while ``blocking=False`` failed at once, and a truthy
value such as ``"false"`` turned a single attempt into a blocking wait.
