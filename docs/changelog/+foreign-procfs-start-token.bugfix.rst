Fix :class:`~filelock.SoftFileLock`, :class:`~filelock.MarkerSoftFileLock` and :class:`~filelock.SoftFileLease` removing
a live holder's marker when the holder or the contender runs in a Linux PID namespace that sees another namespace's
``/proc``, as under ``unshare --pid`` without ``--mount-proc`` or ``nsenter --pid`` without ``--mount``. The start token
read there belongs to an unrelated task, which another user can replace to make the holder look like a recycled PID.
Such a process now records no start token and compares none, so the stale check falls back to the PID alone.
