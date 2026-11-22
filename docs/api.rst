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
    :exclude-members: AcquireReturnProxy, AsyncAcquireReadWriteReturnProxy, AsyncAcquireReturnProxy,
                      AsyncAcquireSoftReadWriteReturnProxy, BaseFileLock, BaseAsyncFileLock, AsyncReadWriteLock,
                      AsyncSoftReadWriteLock, FileLockContext, ReadWriteLock, SoftReadWriteLock, StrictSoftFileLock,
                      CloseErrorPolicy, CompromiseReason, ContextErrorPolicy, OwnerMode, StrictSoftFileClaimState,
                      AcquireWait, InFlight, LeaseClaim, LeaseHeartbeat, StrictRelease, __version__

.. autodata:: filelock.__version__
    :no-value:

.. autodata:: filelock.CloseErrorPolicy

    Controls whether a descriptor close error propagates after the lock gives up ownership.
    See :ref:`how-to:Handle a close failure after unlock`.

.. autodata:: filelock.ContextErrorPolicy

    Controls how context managers report an exception raised inside the block together with a release failure.
    See :ref:`how-to:Reconcile body and release failures`.

.. autodata:: filelock.CompromiseReason

    Reason a lease holder lost its claim, reported by :attr:`~filelock.LeaseCompromise.reason`.

.. autodata:: filelock.OwnerMode

    Lock protocol recorded by :attr:`~filelock.OwnerRecord.mode`. Unrecognized modes read as ``"unknown"``.

.. autodata:: filelock.StrictSoftFileClaimState

    An ``"intent"`` claim is competing for the lock. A ``"held"`` claim records the winner.

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

.. autoclass:: filelock.ReadWriteLock
    :members:

    .. py:attribute:: lock_file

        Path used to identify this lock.

    .. py:attribute:: timeout

        Default acquisition timeout in seconds. With ``-1``, SQLite waits up to its busy-timeout cap of about 23 days.

    .. py:attribute:: blocking

        Whether acquisition waits when another holder owns the lock.

.. autoclass:: filelock.SoftReadWriteLock
    :members: get_lock, generation, compromise, read_lock, acquire_read, release, write_lock, acquire_write, close

    .. py:attribute:: lock_file

        Path used to identify this lock.

    .. py:attribute:: timeout

        Default acquisition timeout in seconds. Use ``-1`` to wait indefinitely.

    .. py:attribute:: blocking

        Whether acquisition waits when another holder owns the lock.

    .. py:attribute:: heartbeat_interval

        Seconds between updates to the holder record.

    .. py:attribute:: stale_threshold

        Seconds a contender must observe an unchanged holder record before eviction.

    .. py:attribute:: poll_interval

        Seconds between acquisition attempts under contention.

.. autoclass:: filelock.StrictSoftFileLock
    :members: claims, lock_path_occupant, release_with_timeout, force_break

.. autoclass:: filelock.AcquireReturnProxy

    .. py:attribute:: lock

        The acquired lock. Exiting the proxy's context releases it.

.. autoclass:: filelock.AsyncAcquireReturnProxy

    .. py:attribute:: lock

        The acquired lock. Exiting the proxy's context releases it.

.. autoclass:: filelock.AsyncAcquireReadWriteReturnProxy

    .. py:attribute:: lock

        The acquired lock. Exiting the proxy's context releases it.

.. autoclass:: filelock.AsyncAcquireSoftReadWriteReturnProxy

    .. py:attribute:: lock

        The acquired lock. Exiting the proxy's context releases it.

Async lock contexts
===================

.. automodule:: filelock.asyncio

.. autoclass:: filelock.asyncio.AsyncFileLockMeta

.. autoclass:: filelock.asyncio.AsyncFileLockContext
    :members: run_in_executor, executor, loop, releasing_through_backend, transition_gate, rollback_tried

.. autoclass:: filelock.asyncio.AsyncThreadLocalFileContext
    :members: current

.. toctree::
    :maxdepth: 1

    context
