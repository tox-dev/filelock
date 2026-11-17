:class:`~filelock.UnixFileLock` refuses, with ``PermissionError``, a lock file that another account owns under a sticky
parent such as ``/tmp``. Its owner could remove it during a hold, after which a second process locked a new file at the
same path and both held the lock. A file owned by the directory's owner, such as ``root`` in ``/tmp``, is still accepted.
