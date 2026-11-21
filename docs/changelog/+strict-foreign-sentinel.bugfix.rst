On POSIX, :class:`StrictSoftFileLock <filelock.StrictSoftFileLock>` rejects another account's compatibility sentinel
in a sticky directory, preserving exclusion with :class:`SoftFileLock <filelock.SoftFileLock>`.
