:class:`~filelock.Timeout` and :attr:`~filelock.StrictSoftFileLock.lock_path_occupant` now escape control characters
and other bytes outside printable non-space ASCII in recorded hostnames.
