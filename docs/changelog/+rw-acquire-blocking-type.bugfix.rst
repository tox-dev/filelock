:class:`~filelock.ReadWriteLock`, :class:`~filelock.SoftReadWriteLock` and their async wrappers now raise
:class:`TypeError` for a *blocking* that is not a :class:`bool`, in the constructor and on acquire, as
:class:`~filelock.FileLock` already does. Before, ``blocking=0`` waited out the full timeout on a contended
:class:`~filelock.ReadWriteLock` while ``blocking=False`` failed at once, and a truthy value such as ``"false"`` turned
a single attempt into a blocking wait.
