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
    :exclude-members: BaseFileLock, BaseAsyncFileLock, AsyncReadWriteLock, AsyncSoftReadWriteLock, StrictSoftFileLock

.. autoclass:: filelock.BaseFileLock
    :members:
    :inherited-members:

.. autoclass:: filelock.BaseAsyncFileLock
    :members: acquire, run_in_executor, executor, release, loop

.. autoclass:: filelock.AsyncReadWriteLock
    :members:
    :inherited-members:

.. autoclass:: filelock.AsyncSoftReadWriteLock
    :members:
    :inherited-members:

.. autoclass:: filelock.StrictSoftFileLock
    :members: claims, lock_path_occupant, release_with_timeout, force_break
