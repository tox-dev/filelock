A raced stale-break rollback now removes the pending marker for :class:`~filelock.SoftFileLock`,
:class:`~filelock.MarkerSoftFileLock`, and :class:`~filelock.SoftFileLease` even if another process replaces the break
name before cleanup.
