###############
 How-to guides
###############

These guides solve specific problems. Each one assumes you're familiar with the basics from :doc:`tutorials`. For design
rationale and trade-offs, see :doc:`concepts`.

**********************
 Handle lock timeouts
**********************

When another process holds a lock, you might want to give up after a certain time rather than waiting forever.

Use the ``timeout`` parameter when acquiring a lock:

.. code-block:: python

    from filelock import FileLock, Timeout

    lock = FileLock("work.lock", timeout=10)

    try:
        with lock:
            # This will wait up to 10 seconds for the lock
            print("Got the lock!")
    except Timeout:
        print("Couldn't get the lock after 10 seconds")

You can also pass ``timeout`` directly to ``acquire()``:

.. code-block:: python

    lock = FileLock("work.lock")

    try:
        with lock.acquire(timeout=5):
            print("Got the lock!")
    except Timeout:
        print("Timeout after 5 seconds")

************************
 Use non-blocking locks
************************

Sometimes you want to try the lock once. Either you get it immediately or you don't.

Set ``blocking=False``:

.. code-block:: python

    from filelock import FileLock, Timeout

    lock = FileLock("work.lock", blocking=False)

    try:
        with lock:
            print("Got the lock immediately")
    except Timeout:
        print("Lock is held by another process")

When ``blocking=False``, the lock makes only one attempt and raises ``Timeout`` if it can't acquire immediately.

The ``blocking`` parameter takes precedence over ``timeout``. If you set both, ``blocking`` wins:

.. code-block:: python

    # This ignores the timeout and tries once
    with lock.acquire(blocking=False, timeout=10):
        pass

**************************
 Control polling interval
**************************

When waiting for a lock, filelock retries at regular intervals. By default it waits 0.05 seconds between attempts.

Increase the poll interval for long-lived locks to reduce CPU usage:

.. code-block:: python

    lock = FileLock("work.lock", poll_interval=0.25)

    with lock:
        # Will check every 0.25 seconds instead of every 0.05 seconds
        pass

Or pass it to ``acquire()``:

.. code-block:: python

    lock = FileLock("work.lock")

    with lock.acquire(poll_interval=1.0):
        # Checks every 1 second
        pass

Change the poll interval anytime via the property:

.. code-block:: python

    lock.poll_interval = 0.5

*****************
 Use async locks
*****************

For async code, use the async variants with ``async with``:

.. code-block:: python

    from pathlib import Path
    from filelock import AsyncFileLock

    lock = AsyncFileLock("work.lock")


    async def read_shared_file():
        async with lock:
            data = Path("data.txt").read_text()
            return data

.. warning::

   ``with`` does not work on async locks. ``acquire`` and ``release`` are coroutines; await them.
   Use ``async with`` as shown above.

By default, async locks run blocking I/O in a thread pool. You can customize this:

.. code-block:: python

    # Use a custom executor
    from concurrent.futures import ThreadPoolExecutor

    executor = ThreadPoolExecutor(max_workers=2)
    lock = AsyncFileLock("work.lock", executor=executor)

    # Or disable executor (only if your filesystem is non-blocking)
    lock = AsyncFileLock("work.lock", run_in_executor=False)

You can also pass a specific event loop:

.. code-block:: python

    import asyncio

    loop = asyncio.new_event_loop()
    lock = AsyncFileLock("work.lock", loop=loop)

Async locks default to ``thread_local=False`` (unlike sync locks which default to ``True``) because the
acquiring and releasing threads may differ when using an executor.

Available async lock classes:

- :class:`AsyncFileLock <filelock.AsyncFileLock>`, platform-aware (recommended).
- :class:`AsyncSoftFileLock <filelock.AsyncSoftFileLock>`.
- :class:`AsyncUnixFileLock <filelock.AsyncUnixFileLock>`.
- :class:`AsyncWindowsFileLock <filelock.AsyncWindowsFileLock>`.

*********************************
 Use locks with multiple threads
*********************************

By default, locks are thread-local. Each thread maintains its own lock state, so nested acquisitions from the same
thread don't block:

.. code-block:: python

    from filelock import FileLock
    import threading

    lock = FileLock("work.lock")  # thread_local=True by default


    def worker():
        with lock:
            print(f"{threading.current_thread().name} has the lock")


    # Each thread can acquire the same lock without blocking
    threading.Thread(target=worker).start()
    worker()  # Main thread

If you need one lock instance shared across threads (and reentrant per thread), set ``thread_local=False``:

.. code-block:: python

    lock = FileLock("work.lock", thread_local=False)
    # Now the lock is reentrant across threads, not per-thread

*********************
 Use singleton locks
*********************

Sometimes you want multiple code paths to reference the same lock without passing it around.

Set ``is_singleton=True``:

.. code-block:: python

    from filelock import FileLock

    # First reference creates the lock
    lock_a = FileLock("work.lock", is_singleton=True)

    # Second reference returns the same instance
    lock_b = FileLock("work.lock", is_singleton=True)

    assert lock_a is lock_b  # Same object

Acquiring through one reference counts toward the same lock depth:

.. code-block:: python

    lock_a.acquire()
    lock_b.acquire()  # reentrant, lock counter is now 2
    lock_b.release()  # Lock counter is 1
    lock_a.release()  # Lock is fully released

Parameters are frozen when the singleton is first created. Requesting with different parameters raises ``ValueError``:

.. code-block:: python

    lock1 = FileLock("work.lock", is_singleton=True, timeout=10)
    lock2 = FileLock("work.lock", is_singleton=True, timeout=5)  # ValueError!

*****************************************
 Use shared read / exclusive write locks
*****************************************

When you have many readers and occasional writers, use :class:`ReadWriteLock <filelock.ReadWriteLock>` to allow readers
to proceed concurrently. The lock file must use a ``.db`` extension because ``ReadWriteLock`` is backed by SQLite:

.. code-block:: python

    from filelock import ReadWriteLock

    rw = ReadWriteLock("data.db")

    # Multiple processes can read simultaneously
    with rw.read_lock():
        data = get_shared_data()

    # Only one process can write at a time
    with rw.write_lock():
        update_shared_data()

You can pass ``timeout`` and ``blocking`` to the context managers:

.. code-block:: python

    with rw.read_lock(timeout=5):
        data = get_shared_data()

    with rw.write_lock(timeout=10, blocking=True):
        update_shared_data()

``ReadWriteLock`` is singleton by default (``is_singleton=True``). Calling ``ReadWriteLock("data.db")`` with the same
path returns the same instance, unlike ``FileLock`` which defaults to ``is_singleton=False``.

Use low-level methods for more control:

.. code-block:: python

    rw.acquire_read(timeout=5)
    try:
        data = get_shared_data()
    finally:
        rw.release()

    rw.acquire_write(timeout=5)
    try:
        update_shared_data()
    finally:
        rw.release()

Read locks are reentrant from the same thread:

.. code-block:: python

    with rw.read_lock():
        with rw.read_lock():  # OK
            pass

Write locks are also reentrant from the same thread:

.. code-block:: python

    with rw.write_lock():
        with rw.write_lock():  # OK
            pass

But upgrading from read to write (or downgrading) raises an error:

.. code-block:: python

    with rw.read_lock():
        with rw.write_lock():  # RuntimeError
            pass

``ReadWriteLock`` opens a SQLite connection for an outer acquisition and closes it after the final matching
``release()``. Reentrant acquisitions share that transaction. Call ``close()`` to release a held lock and invalidate the
instance:

.. code-block:: python

    rw = ReadWriteLock("data.db")
    try:
        with rw.read_lock():
            data = get_shared_data()
    finally:
        rw.close()

SQLite prohibits using or closing a database connection inherited across ``fork()``. filelock invalidates an
inherited ``ReadWriteLock`` or ``AsyncReadWriteLock`` in the child. Acquisition raises ``RuntimeError``; ``release()``
and ``close()`` do nothing so context-manager cleanup cannot release the parent's transaction. On CPython, if no
SQLite connection was active at the fork, construct a new lock in the child before acquiring:

.. code-block:: python

    child_rw = ReadWriteLock("data.db")
    with child_rw.read_lock():
        data = get_shared_data()

See SQLite's `fork guidance`_.
PyPy can leave process-wide SQLite state unsafe after a fork even when every known connection was closed. If the parent
used SQLite after importing filelock, a PyPy child therefore rejects every ``ReadWriteLock`` and
``AsyncReadWriteLock`` until ``exec()`` or exit. Importing filelock only in the child cannot detect SQLite use that
happened in the parent.

If a connection was active, filelock rejects every lock for the same path or database inode in the child. In the normal
path, an outer acquisition owns this connection; failed cleanup can also retain one. The child must call ``exec()`` or
exit before using that database. filelock abandons each inherited active handle until OS process exit, consuming one
descriptor and its SQLite memory per active lock. CPython 3.10 and 3.11 need an extra raw reference to stop their base
deallocator from closing the handle; later versions suppress finalization through the connection subclass. Both paths
avoid the ``sqlite3_close()`` call that SQLite forbids after a fork.

filelock normally rejects ``fork()`` from a callback that runs during a SQLite operation. If another audit hook blocks
that guard from registering, a child created at this boundary exits with status 70 before it can touch the inherited
connection; the parent operation continues.

A native or signal callback that forks while its thread is executing inside SQLite can resume in inherited C state;
filelock cannot make that operation safe.

.. _fork guidance: https://www.sqlite.org/howtocorrupt.html#_carrying_an_open_database_connection_across_a_fork_

********************************************
 Use read/write locks on shared filesystems
********************************************

:class:`ReadWriteLock <filelock.ReadWriteLock>` is SQLite-backed and requires a local filesystem supported by the active
SQLite VFS. On a shared filesystem, use :class:`SoftReadWriteLock <filelock.SoftReadWriteLock>` only after verifying
exclusive creation, rename, unlink, timestamps, and cache visibility across participating hosts. Its heartbeat permits
a peer to evict a marker after ``stale_threshold`` even if the old holder later resumes.

.. code-block:: python

    from filelock import SoftReadWriteLock

    rw = SoftReadWriteLock("/shared/nfs/data.lock")

    with rw.read_lock():
        data = get_shared_data()

    with rw.write_lock():
        update_shared_data()

The defaults (``heartbeat_interval=30`` s, ``stale_threshold=90`` s, ``poll_interval=0.25`` s) fit workloads
that hold locks for seconds-to-minutes. Tune them for your deployment:

.. code-block:: python

    rw = SoftReadWriteLock(
        "/shared/nfs/data.lock",
        heartbeat_interval=30,   # how often to refresh the marker's mtime
        stale_threshold=90,      # declare a marker stale after this many seconds of no refresh
        poll_interval=0.25,      # how long to sleep between acquire retries
    )

Pick ``stale_threshold`` larger than any realistic process or filesystem pause. ``heartbeat_interval`` should be
roughly ``stale_threshold / 3``. Lower ``poll_interval`` reduces acquisition latency at the cost of more filesystem
metadata calls. Synchronize participating clocks and fence protected writes if an expired holder can resume.

Writer acquisition is two-phase and writer-preferring: phase one claims the writer marker (which blocks any
new reader), phase two waits for existing readers to drain. This rules out writer starvation under read-heavy
workloads. See :doc:`concepts` for the full model.

**Fork caveat.** A process that forks while holding a ``SoftReadWriteLock`` loses the lock in the child. filelock marks
the inherited instance fork-invalidated; ``release()`` on it becomes a no-op, and the child must construct a fresh
``SoftReadWriteLock(path)`` before acquiring. This follows the invalidation approach used by PyMongo's connection
pools.

**Trust boundary.** The class coordinates cooperating processes. Directory ownership, ACLs, and ``0o600`` / ``0o700``
permissions can exclude another UID. A process with the same effective UID can alter the markers, and incorrect clocks
or cache behavior can trigger expiry while a holder remains active.

***********************************
 Use async read / write locks
***********************************

For async code, use :class:`AsyncReadWriteLock <filelock.AsyncReadWriteLock>`. Because Python's :mod:`sqlite3` module
has no async API, it wraps :class:`ReadWriteLock <filelock.ReadWriteLock>` and dispatches all blocking SQLite operations
to a thread pool via ``loop.run_in_executor``:

.. code-block:: python

    from filelock import AsyncReadWriteLock

    rw = AsyncReadWriteLock("data.db")

    async with rw.read_lock():
        data = await get_shared_data()

    async with rw.write_lock():
        await update_shared_data()

You can pass a custom executor:

.. code-block:: python

    from concurrent.futures import ThreadPoolExecutor

    executor = ThreadPoolExecutor(max_workers=2)
    rw = AsyncReadWriteLock("data.db", executor=executor)

Low-level ``acquire_read``/``acquire_write``/``release`` methods are also available:

.. code-block:: python

    await rw.acquire_read(timeout=5)
    try:
        data = await get_shared_data()
    finally:
        await rw.release()

The same reentrancy and upgrade/downgrade rules as the synchronous :class:`ReadWriteLock <filelock.ReadWriteLock>`
apply. See :ref:`how-to:Use shared read / exclusive write locks` for details.

For a shared filesystem whose marker and cache behavior has been verified, use
:class:`AsyncSoftReadWriteLock <filelock.AsyncSoftReadWriteLock>`, which wraps
:class:`SoftReadWriteLock <filelock.SoftReadWriteLock>` the same way:

.. code-block:: python

    from filelock import AsyncSoftReadWriteLock

    rw = AsyncSoftReadWriteLock("/shared/nfs/data.lock")

    async with rw.read_lock():
        data = await get_shared_data()

**************************************
 Detect stale locks (soft locks only)
**************************************

:class:`SoftFileLock <filelock.SoftFileLock>` stores the PID and hostname of the lock holder. A same-host contender may
remove the marker when it can prove that PID does not exist.

This happens automatically. You don't need to do anything special:

.. code-block:: python

    from filelock import SoftFileLock

    lock = SoftFileLock("work.lock")

    with lock:
        # If the process holding the lock dies,
        # another process will automatically clean up the stale lock
        pass

Cross-host records remain because their PID cannot be interpreted locally. On platforms without process-start identity,
a reused PID can also keep a dead owner's marker in place.

On Windows, the marker also stores process creation time to guard against PID recycling. Malformed records follow a
different rule: a waiter may evict them after two seconds. That recovery path is not fail closed.

*****************************************
 Inspect and manage PID locks
*****************************************

:class:`SoftFileLock <filelock.SoftFileLock>` exposes properties to inspect the lock holder and a method to forcibly
break the lock. This is useful for migrating from the deprecated `lockfile <https://pypi.org/project/lockfile/>`_
library's ``PIDLockFile`` class.

Read the PID of the current lock holder:

.. code-block:: python

    from filelock import SoftFileLock

    lock = SoftFileLock("work.lock")

    with lock:
        print(lock.pid)  # e.g. 12345

    print(lock.pid)  # None (lock file removed after release)

Check whether the current process holds the lock:

.. code-block:: python

    lock = SoftFileLock("work.lock")

    print(lock.is_lock_held_by_us)  # False

    with lock:
        print(lock.is_lock_held_by_us)  # True

Forcibly break a lock regardless of who holds it:

.. code-block:: python

    lock = SoftFileLock("work.lock")
    lock.break_lock()  # removes the lock file unconditionally

*****************
 Control logging
*****************

All log messages use the ``DEBUG`` level under the ``filelock`` logger name. Control logging via Python's standard
library:

.. code-block:: python

    import logging

    # Hide filelock debug messages
    logging.getLogger("filelock").setLevel(logging.INFO)

    # Or show all messages
    logging.getLogger("filelock").setLevel(logging.DEBUG)

    # Configure a handler to see them
    handler = logging.StreamHandler()
    logging.getLogger("filelock").addHandler(handler)

*******************************
 Choose a soft-lock contract
*******************************

:class:`StrictSoftFileLock <filelock.StrictSoftFileLock>` never reclaims a marker. A malformed record, an owner on
another host, a dead PID and an old marker all read as held, so acquisition waits rather than overlap a holder that may
still be alive. A crashed holder leaves a marker no contender removes; clear it with
:meth:`force_break <filelock.MarkerSoftFileLock.force_break>`, which voids mutual exclusion for whoever is still
running.

:class:`SoftFileLease <filelock.SoftFileLease>` trades exclusion for progress. The holder refreshes its claim, and a
peer takes the marker once it is ``lease_duration`` seconds stale. The expired holder keeps running and keeps using
whatever the lock protects, so a lease says who *should* be working, not who alone is. ``on_compromise`` fires when the
claim is lost. :attr:`token <filelock.SoftFileLease.token>` names a claim but does not fence one: to reject a superseded
holder, the protected resource must be linearizable and fence on a monotonic generation it controls.

.. code-block:: python

    from filelock import SoftFileLease, StrictSoftFileLock

    with StrictSoftFileLock("work.lock", timeout=30):
        pass  # no peer enters while this holder lives

    def stop_working(compromise):
        print("lost the claim:", compromise.reason)

    with SoftFileLease("work.lock", lease_duration=60, on_compromise=stop_working):
        pass  # a peer may enter 60s after the last refresh

Every contender for a path must agree on ``lease_duration``; one that disagrees raises
:class:`LeaseSettingsMismatch <filelock.LeaseSettingsMismatch>` instead of applying its own expiry to a peer that never
agreed to it. Native locks reject lease settings, because pathname age cannot revoke a kernel lock on an inode. Async
callers use ``AsyncStrictSoftFileLock`` and ``AsyncSoftFileLease``.

Windows refuses to rename or delete a file another process holds open, so a peer takes an expired claim there only once
the previous holder's process exits and its handle closes. A holder that keeps running but stops refreshing keeps its
marker on Windows, while Unix lets a peer reclaim it after ``lease_duration``.

**Do not mix contracts on one path.** Both classes publish a protocol 2 record.
:class:`SoftFileLock <filelock.SoftFileLock>` reads that record as malformed and evicts it once past its grace period,
so a legacy contender can delete a live strict marker or a live lease. Only these combinations hold:

.. list-table:: Mutual exclusion between contenders
    :header-rows: 1

    - - Contenders on one path
      - Holds?
    - - ``StrictSoftFileLock`` with ``StrictSoftFileLock``
      - Yes.
    - - ``SoftFileLease`` with ``SoftFileLease``, same ``lease_duration``
      - Until a claim expires; then the old holder overlaps its successor.
    - - ``SoftFileLock`` with ``StrictSoftFileLock`` or ``SoftFileLease``
      - No. The legacy contender evicts the protocol 2 marker.
    - - ``StrictSoftFileLock`` with ``SoftFileLease``
      - The lease waits out a strict holder, but a strict contender never reclaims an expired lease.

***********************************
 Configure legacy age-based expiry
***********************************

Only :class:`SoftFileLock <filelock.SoftFileLock>` honors ``lifetime``. The value sets the marker age that permits
removal. A waiter may enter after that age even while the previous holder continues its protected operation:

.. code-block:: python

    from filelock import SoftFileLock

    # A waiter may remove this marker after one hour.
    lock = SoftFileLock("work.lock", lifetime=3600)

    with lock:
        # This operation can overlap a successor after the marker expires.
        pass

Constructing or assigning a non-``None`` value emits
:class:`SoftFileLockLifetimeWarning <filelock.SoftFileLockLifetimeWarning>`. Migrate to ``SoftFileLease`` when expiry
is required or ``StrictSoftFileLock`` when unknown and stale claims must fail closed. Async callers use
``AsyncSoftFileLease`` or ``AsyncStrictSoftFileLock``. ``lifetime=None`` disables age-based removal; same-host dead-PID
and malformed-record recovery still apply.

.. list-table:: ``lifetime`` behavior
    :header-rows: 1

    - - Backend and value
      - Behavior
      - Mutual-exclusion limit
    - - ``SoftFileLock``, ``None``
      - Disables age-based removal.
      - Shared-marker recovery and forced breaking can still remove the marker.
    - - ``SoftFileLock``, non-``None``
      - Removes a marker after the configured age.
      - A live holder may overlap its successor.
    - - Native lock, non-``None``
      - Emits a warning and ignores the value.
      - Kernel lock ownership is unchanged.

Native locks (:class:`UnixFileLock <filelock.UnixFileLock>` and
:class:`WindowsFileLock <filelock.WindowsFileLock>`) ignore a non-``None`` value and emit a warning. The same is true
when :class:`FileLock <filelock.FileLock>` selects one of those backends; on a build without ``fcntl``, ``FileLock`` may
instead alias ``SoftFileLock``. A kernel lock lives on the inode, so pathname age cannot revoke it.

**************************
 Cancel lock acquisition
**************************

You can interrupt a waiting ``acquire()`` by passing a ``cancel_check`` callable. The lock polls this function between
retry attempts and raises :class:`Timeout <filelock.Timeout>` when it returns ``True``:

.. code-block:: python

    import threading
    from filelock import FileLock, Timeout

    shutdown = threading.Event()
    lock = FileLock("work.lock")

    try:
        with lock.acquire(timeout=-1, cancel_check=shutdown.is_set):
            print("Got the lock")
    except Timeout:
        print("Acquisition canceled")

    # From another thread:
    shutdown.set()  # causes the acquire loop to stop

This is useful in long-running services where you need to shut down gracefully without waiting for a lock that may never
become available.

.. mermaid::

    sequenceDiagram
        participant W as Worker Thread
        participant L as File Lock
        participant M as Main Thread
        W->>L: acquire(cancel_check=shutdown.is_set)
        loop Every poll_interval
            L->>L: Try lock (busy)
            L->>W: Check cancel_check()
            W-->>L: False (keep waiting)
        end
        M->>M: shutdown.set()
        L->>W: Check cancel_check()
        W-->>L: True (cancel!)
        L->>W: Raise Timeout
        Note over W: Clean shutdown

***********************
 Force-release a lock
***********************

When a lock is acquired multiple times (reentrant), ``release()`` only decrements the counter. To immediately release
regardless of the counter, pass ``force=True``:

.. code-block:: python

    from filelock import FileLock

    lock = FileLock("work.lock")

    lock.acquire()
    lock.acquire()
    print(lock.lock_counter)  # 2

    lock.release(force=True)
    print(lock.is_locked)  # False, fully released

This is useful in error recovery or cleanup handlers where you need to ensure the lock is fully released:

.. code-block:: python

    import signal

    lock = FileLock("work.lock")


    def cleanup(signum, frame):
        lock.release(force=True)
        raise SystemExit(1)


    signal.signal(signal.SIGTERM, cleanup)

*******************************
 Check your own lock state
*******************************

Use the :attr:`~filelock.BaseFileLock.is_locked` property to check whether *your* lock instance currently holds the
lock, and :attr:`~filelock.BaseFileLock.lock_counter` to see the reentrant depth:

.. code-block:: python

    from filelock import FileLock

    lock = FileLock("work.lock")

    if not lock.is_locked:
        with lock:
            print(f"Lock depth: {lock.lock_counter}")

These properties reflect the state of your lock instance only. To check if *another* process holds the lock, try to
acquire with ``blocking=False``:

.. code-block:: python

    from filelock import FileLock, Timeout

    lock = FileLock("work.lock")

    try:
        with lock.acquire(blocking=False):
            print("Lock was free")
    except Timeout:
        print("Lock is held by another process")

***************************************
 Reconcile body and release failures
***************************************

When a ``with`` block fails and releasing the lock on exit also fails, Python's default keeps the body error in the
release error's ``__context__``. That buries one error inside the other. Set ``context_error_policy="group"`` to raise
both as siblings of a :class:`BaseExceptionGroup`, body first, release second:

.. code-block:: python

    from filelock import FileLock

    lock = FileLock("work.lock", context_error_policy="group")

    with lock:
        raise RuntimeError("body failed")
    # if release() then also fails, both surface as a BaseExceptionGroup

``"group"`` needs Python 3.11+ or the ``exceptiongroup`` backport; filelock checks this when you construct the lock.
When both errors subclass :class:`Exception`, the group is a plain :class:`ExceptionGroup`, so ``except*`` and
``except Exception`` still catch it. The default ``"chain"`` keeps Python's behavior.

*****************************************
 Handle a close failure after unlock
*****************************************

Native locks close their descriptor after the OS unlock commits. Soft locks close the marker descriptor after they
capture its identity for safe cleanup. ``os.close`` can fail even though filelock has relinquished ownership;
``close_error_policy`` decides the outcome:

- ``"default"`` keeps historical behavior: Unix native locks drop the error, while Windows native and soft locks
  propagate it.
- ``"raise"`` always propagates the ``OSError``.
- ``"suppress"`` always ignores it.

.. code-block:: python

    from filelock import FileLock, SoftFileLock

    native_lock = FileLock("native.lock", close_error_policy="suppress")
    soft_lock = SoftFileLock("soft.lock", close_error_policy="suppress")

Filelock relinquishes descriptor ownership before applying this policy and never retries the descriptor number. The
policy does not affect native unlock failures or marker deletion. A soft lock still attempts identity-checked marker
cleanup after a close error.

***********************************************
 Fail closed instead of downgrading to soft
***********************************************

On Unix, when ``flock`` returns ``ENOSYS``, :class:`UnixFileLock <filelock.UnixFileLock>` switches to
:class:`SoftFileLock <filelock.SoftFileLock>` semantics by default. If the application requires the native backend,
construct ``UnixFileLock`` with ``fallback_to_soft=False`` so ``ENOSYS`` propagates:

.. code-block:: python

    from filelock import UnixFileLock

    lock = UnixFileLock("work.lock", fallback_to_soft=False)

This option does not change ``FileLock`` on a build where the alias is already ``SoftFileLock`` because ``fcntl`` is
unavailable. It has no effect on Windows or an explicitly constructed ``SoftFileLock``.

*********************************
 Keep the lock file on release
*********************************

Native backends handle the lock pathname differently: Windows unlinks it after release, Unix leaves it in place. Pass
``preserve_lock_file=True`` for a stable file identity across releases, which matters for ACLs, auditing, or holder
metadata written through :ref:`on_acquired <how-to:Run a callback once the lock is held>`:

.. code-block:: python

    from filelock import FileLock

    lock = FileLock("work.lock", preserve_lock_file=True)

Windows then skips its post-release unlink, and Unix refuses the ``ENOSYS`` soft fallback (which releases by
unlinking). :class:`SoftFileLock <filelock.SoftFileLock>` rejects ``True`` because unlinking its marker is how it
releases. The promise covers filelock's own release path; it cannot stop another process or the filesystem from
removing the file.

*****************************************
 Run a callback once the lock is held
*****************************************

``on_acquired`` runs once per physical acquisition, after filelock holds the native lock and finished backend
initialization, before :meth:`acquire() <filelock.BaseFileLock.acquire>` returns. filelock passes the borrowed lock
descriptor. The callback may read,
write, seek, truncate, or set metadata through ``os`` on it, but must not close, unlock, or take ownership of the
descriptor. A recursive acquire does not call it again. If the callback raises, filelock releases the lock and
re-raises.

A common use is stamping holder metadata into the lock file:

.. code-block:: python

    import json
    import os
    import socket

    from filelock import FileLock


    def write_holder(fd: int) -> None:
        os.ftruncate(fd, 0)
        os.lseek(fd, 0, os.SEEK_SET)
        os.write(fd, json.dumps({"pid": os.getpid(), "host": socket.gethostname()}).encode())


    lock = FileLock("work.lock", on_acquired=write_holder, preserve_lock_file=True)

    with lock:
        ...  # while held, other processes can read the holder metadata from work.lock

filelock does not ``fsync`` the descriptor's writes. Native locks only; :class:`SoftFileLock
<filelock.SoftFileLock>` rejects the hook because it keeps its own protocol state in the marker file.

*******************************************
 Lock a descriptor you already own
*******************************************

:func:`lock_descriptor <filelock.lock_descriptor>` and :func:`unlock_descriptor <filelock.unlock_descriptor>` take and
release the same one-byte native lock :class:`FileLock <filelock.FileLock>` uses, but on a file descriptor you opened
and own. They add no path handling: no open, truncate, close, unlink, chmod, canonicalize, or fallback. A descriptor
lock and a ``FileLock`` path lock on the same file contend with each other.

.. code-block:: python

    import os

    from filelock import lock_descriptor, unlock_descriptor

    fd = os.open("work.lock", os.O_RDWR | os.O_CREAT)
    try:
        lock_descriptor(fd)  # blocks until the lock is held
        try:
            ...  # critical section
        finally:
            unlock_descriptor(fd)
    finally:
        os.close(fd)

Pass ``blocking=False`` for a single attempt that returns ``False`` on contention and ignores ``poll_interval``.
Blocking calls require a finite, positive ``poll_interval``. There is no async wrapper. Run it in an executor, or drive
``blocking=False`` from your own polling loop. On Windows *fd* must be a synchronous descriptor.

Both functions raise ``OSError`` with :data:`errno.ENOSYS` when the Python build lacks the native locking primitive.
The :class:`FileLock <filelock.FileLock>` and :class:`AsyncFileLock <filelock.AsyncFileLock>` aliases continue to select
their soft implementations on those builds. For timeout, reentrancy, singleton, lifetime, or stale-break behavior, use
:class:`FileLock <filelock.FileLock>`.
