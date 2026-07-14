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

When you're done with a ``ReadWriteLock``, close it to release the underlying SQLite connection:

.. code-block:: python

    rw = ReadWriteLock("data.db")
    try:
        with rw.read_lock():
            data = get_shared_data()
    finally:
        rw.close()  # releases any held lock and closes the SQLite connection

***************************************************
 Use read/write locks on network filesystems (NFS)
***************************************************

:class:`ReadWriteLock <filelock.ReadWriteLock>` is SQLite-backed and requires a local filesystem: SQLite's own
docs warn against running on NFS because POSIX ``fcntl`` locks are unreliable there. For HPC clusters, slurm
deployments, or any multi-host shared storage, use :class:`SoftReadWriteLock <filelock.SoftReadWriteLock>`
instead. It is built on :class:`SoftFileLock <filelock.SoftFileLock>` primitives (atomic ``O_CREAT | O_EXCL | O_NOFOLLOW``) and runs a
daemon heartbeat thread that refreshes each held marker's ``mtime`` so any host on any node can evict a stale
marker when the holder crashes.

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

Pick ``stale_threshold`` larger than any realistic pause a holder could experience (GC, disk flush, kernel
preemption). ``heartbeat_interval`` should be roughly ``stale_threshold / 3``; that is the ratio etcd uses for
its ``LeaseKeepAlive``. Lower ``poll_interval`` reduces acquire latency under contention at the cost of more
NFS ``stat`` calls per waiting client.

Writer acquisition is two-phase and writer-preferring: phase one claims the writer marker (which blocks any
new reader), phase two waits for existing readers to drain. This rules out writer starvation under read-heavy
workloads. See :doc:`concepts` for the full model.

**Fork caveat.** A process that forks while holding a ``SoftReadWriteLock`` loses the lock in the child. filelock marks the
inherited instance fork-invalidated; ``release()`` on it becomes a no-op, and the child must call
``SoftReadWriteLock(path)`` again to get a fresh instance before acquiring. Matches the semantics of
:class:`threading.Lock` and PyMongo's connection pools.

**Trust boundary.** The class protects against same-UID non-cooperating processes on one host, cross-host
same-UID processes, and same-host different-UID users (via ``0o600`` / ``0o700`` permissions). It does not
protect against root compromise, NTP tampering on same-UID cross-host nodes, or multi-tenant mounts where
hostile co-tenants share the UID.

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

For network filesystems, use :class:`AsyncSoftReadWriteLock <filelock.AsyncSoftReadWriteLock>`, which wraps
:class:`SoftReadWriteLock <filelock.SoftReadWriteLock>` the same way:

.. code-block:: python

    from filelock import AsyncSoftReadWriteLock

    rw = AsyncSoftReadWriteLock("/shared/nfs/data.lock")

    async with rw.read_lock():
        data = await get_shared_data()

**************************************
 Detect stale locks (soft locks only)
**************************************

:class:`SoftFileLock <filelock.SoftFileLock>` stores the PID and hostname of the lock holder. It can detect when the
holding process has died and break stale locks on all platforms.

This happens automatically. You don't need to do anything special:

.. code-block:: python

    from filelock import SoftFileLock

    lock = SoftFileLock("work.lock")

    with lock:
        # If the process holding the lock dies,
        # another process will automatically clean up the stale lock
        pass

Stale lock detection only detects locks from the same host. Cross-host stale locks still require manual removal.

On Windows, the lock file additionally stores the process creation time to guard against PID recycling. filelock evicts
malformed lock files (empty or corrupted) after a brief safety window.

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

*******************
 Set lock lifetime
*******************

Only :class:`SoftFileLock <filelock.SoftFileLock>` honors ``lifetime``. A waiting process breaks a lock file whose
modification time is older than ``lifetime`` seconds, even if the holder is still alive:

.. code-block:: python

    from filelock import SoftFileLock

    # Lock expires after 3600 seconds (1 hour)
    lock = SoftFileLock("work.lock", lifetime=3600)

    with lock:
        # Lock is held; a waiter may break it once the lock file is older than 1 hour
        pass

This helps distributed systems where a process might crash and leave a lock behind. After the lifetime expires, other
processes can acquire it.

Native locks (:class:`FileLock <filelock.FileLock>`, :class:`UnixFileLock <filelock.UnixFileLock>`,
:class:`WindowsFileLock <filelock.WindowsFileLock>`) ignore a non-``None`` ``lifetime`` and emit a warning. A kernel
lock lives on the inode, so unlinking the pathname by age cannot revoke it, and a contender would lock a fresh inode
while the holder is still live. Use ``SoftFileLock`` when you need age-based expiry.

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

On Unix, when the filesystem's ``flock`` returns ``ENOSYS`` (some network mounts), :class:`FileLock
<filelock.FileLock>` switches to :class:`SoftFileLock <filelock.SoftFileLock>` semantics by default, trading
kernel-enforced locking for cooperative existence locking. If your code needs kernel enforcement, pass
``fallback_to_soft=False`` so the ``ENOSYS`` propagates instead of a silent downgrade to soft locking:

.. code-block:: python

    from filelock import FileLock

    lock = FileLock("work.lock", fallback_to_soft=False)

It has no effect on Windows or :class:`SoftFileLock <filelock.SoftFileLock>`.

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
