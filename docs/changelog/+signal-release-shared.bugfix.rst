A raising signal handler during :class:`~filelock.FileLock` release on an instance shared across threads no longer
unlocks a hold another thread acquired after the interrupted release.
