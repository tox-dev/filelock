``import filelock`` no longer imports :mod:`asyncio`, :mod:`sqlite3` or the soft, strict, lease and read-write lock
modules. Each loads on first access to a name that needs it, which roughly halves the import time of a program that
only uses :class:`~filelock.FileLock`.
