On POSIX, :class:`StrictSoftFileLock <filelock.StrictSoftFileLock>` now refuses a compatibility sentinel owned by
another account under a sticky parent. Its owner could remove the sentinel during a strict hold, letting a
:class:`SoftFileLock <filelock.SoftFileLock>` enter despite the strict claims.
