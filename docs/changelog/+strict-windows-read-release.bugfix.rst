On Windows, :class:`~filelock.StrictSoftFileLock` honors the acquire timeout while pinning directories, publishing
claims or withdrawing after a lost race. A worker finishes delayed withdrawal. Interrupted publication can leave
private records requiring manual removal. Release waits for Windows to close both public claims.
