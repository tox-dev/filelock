:class:`~filelock.Timeout` and :attr:`~filelock.StrictSoftFileLock.lock_path_occupant` now show the hostname a marker
records within the grammar markers are written in, escaping every byte outside printable non-space ASCII as ``?<hex>``.
A marker written by a release before the hostname escape, or by another local user who created the lock file first,
could put a terminal escape sequence or other control bytes into the message, which then erased or rewrote the lines
around it on the terminal or in the log that printed it.
