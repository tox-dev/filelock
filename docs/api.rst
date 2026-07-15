###############
 API Reference
###############

This section documents all public classes, exceptions, and attributes. For usage examples and task-oriented guidance,
see :doc:`tutorials` and :doc:`how-to`.

:class:`~filelock.FileLock` and :class:`~filelock.AsyncFileLock` are platform aliases: at import time they resolve to
:class:`~filelock.UnixFileLock` or :class:`~filelock.WindowsFileLock` (and their async peers), or to the soft backends on
a build without ``fcntl``. The shared ``acquire`` / ``release`` / ``timeout`` interface they expose lives on
:class:`~filelock.BaseFileLock` and :class:`~filelock.BaseAsyncFileLock` below.

.. automodule:: filelock
    :members:
    :show-inheritance:
