A read-handle oplock on an abandoned private record no longer holds :class:`~filelock.StrictSoftFileLock` acquisition
past its timeout on Windows. Windows retains abandoned private records for manual cleanup.
