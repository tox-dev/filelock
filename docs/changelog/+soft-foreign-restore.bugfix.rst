On POSIX systems with atomic name exchange, :class:`~filelock.SoftFileLock` preserves the live holder's marker when
recovery encounters another account's record in a sticky directory.
