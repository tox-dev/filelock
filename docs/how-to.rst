###############
 How-to guides
###############

These guides solve specific problems. Each one assumes you're familiar with the basics from :doc:`tutorials`. For design
rationale and trade-offs, see :doc:`concepts`.

*************************************
 Name and place the lock file
*************************************

Lock a sidecar, not the resource itself. Opening the target to lock it creates it, which destroys the distinction a
cache guard depends on: whether the file exists yet. On Windows an open handle also blocks the rename or unlink you were
about to perform. The convention is ``<resource>.lock`` beside the resource, or a parallel lock tree. ``huggingface_hub``
keeps its cache under ``<cache>/models/...`` and its locks under ``<cache>/.locks/...``.

Keep the name within the filesystem's limit. A path component caps at 255 bytes on ext4 and 255 Unicode characters on
`NTFS <https://learn.microsoft.com/en-us/windows/win32/fileio/filesystem-functionality-comparison>`_. A cache key pasted
into a lock name can exceed either limit:

.. code-block:: python

    import hashlib
    import os

    from filelock import FileLock


    def lock_for(cache_dir: str, key: str) -> FileLock:
        name = f"{hashlib.sha256(key.encode('utf-8', 'surrogatepass')).hexdigest()}.lock"
        return FileLock(os.path.join(cache_dir, name))

``datasets`` hashes over-long lock names against ``os.statvfs(path).f_namemax`` for exactly this reason. A hash also
sidesteps the separators, spaces, and non-portable characters a natural key tends to carry. Hash short keys too: an
absolute key would otherwise discard ``cache_dir`` when joined, and a short key can still contain characters Windows
does not permit in filenames. The SHA-256 name above is 69 ASCII bytes including ``.lock`` and does not require the
cache directory to exist before constructing the lock. The explicit encoding also accepts surrogate escapes from Unix
filenames with undecodable bytes.

Two more constraints worth designing around:

- **All contenders must agree on the path.** A lock is a rendezvous on one pathname. Resolve symlinks and relative
  paths the same way everywhere, or two processes will politely lock different files. Bind-mounted containers make this
  easy to get wrong: the same file, two paths, no exclusion.
  filelock resolves a relative path when an acquisition starts and keeps that path until the hold ends. Do not rename
  the lock's directory while a lock in it is held or waited on: a waiter then fails with :class:`FileNotFoundError`.
- **Create the resource's directory when using a separate lock tree.** filelock creates the lock file's parent
  directories automatically, but it does not create directories for the resource being protected.

*****************************************
 Keep correctness independent of the lock
*****************************************

An advisory lock binds only the processes that ask for it. A different tool, an older version, an ``ENOLCK`` on a
network mount, or an operator with ``rm`` proceeds regardless. For a cache, this argues for a design where the lock is
an *optimization* and the filesystem supplies the correctness:

.. code-block:: python

    import os
    import tempfile
    from pathlib import Path

    from filelock import FileLock, Timeout


    def populate(target: Path, produce) -> None:
        fd, tmp = tempfile.mkstemp(dir=target.parent)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(produce())
            os.chmod(tmp, 0o644)  # mkstemp creates 0600; widen before publishing
            os.replace(tmp, target)  # atomic: readers see the old file or the new one, never a partial one
        except BaseException:
            os.unlink(tmp)
            raise


    def get(target: Path, produce) -> bytes:
        lock = FileLock(f"{target}.lock")
        try:
            with lock.acquire(timeout=30):
                if not target.exists():
                    populate(target, produce)
        except Timeout:
            if not target.exists():  # the lock only saved duplicate work; do the work anyway
                populate(target, produce)
        return target.read_bytes()

``os.replace`` is atomic within a filesystem, so a reader always observes a complete file. The lock stops two processes
from *both* producing; losing it costs duplicated work, not a corrupted cache. ``huggingface_hub`` says as much in its
cache code, where a comment records that the lock is best-effort and that cache correctness does not depend on it.

`pip <https://github.com/pypa/pip>`_ takes the same position by omission. It uses no file locking at all, relying on
atomic replace and documenting the duplicated-download race it accepts.

Reach for real exclusion (:class:`StrictSoftFileLock <filelock.StrictSoftFileLock>` or a native
:class:`FileLock <filelock.FileLock>`) when losing the lock costs more than repeated work: a non-idempotent migration,
an append to a shared file, a resource that cannot be produced twice.

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

A negative ``timeout`` or ``math.inf`` waits without a limit, and a blocking acquire raises :class:`ValueError` for
``nan``. A ``timeout`` may be any real number, including :class:`~decimal.Decimal` and :class:`~fractions.Fraction`; a
:class:`bool` raises :class:`TypeError`.

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
:class:`~filelock.StrictSoftFileLock` and :class:`~filelock.AsyncStrictSoftFileLock` publish several files per
attempt. With a ``poll_interval`` below 0.05 seconds, each retry waits a random time between ``poll_interval`` and an
upper bound that starts at twice ``poll_interval`` and doubles after each failed attempt, up to 0.05 seconds, so
contending processes stop retrying in step.

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

************************************
 Probe a lock before blocking on it
************************************

A lock that is free costs nothing to take. A lock that is held can stall a user in silence for minutes with no clue why.
Probe first, and you can tell them:

.. code-block:: python

    import logging

    from filelock import FileLock, Timeout

    lock = FileLock("py_info/5/a1b2c3.json.lock")

    try:
        lock.acquire(blocking=False)
    except Timeout:
        logging.info("lock held by another process, waiting for it to release %s", lock.lock_file)
        lock.acquire()  # now block for as long as it takes

    try:
        ...  # critical section
    finally:
        lock.release()

The first ``acquire`` returns immediately in the common uncontended case. Only when it raises do you log and settle in
to wait, so the message appears exactly when it is useful and never otherwise.

`virtualenv <https://github.com/pypa/virtualenv>`_, `pre-commit <https://github.com/pre-commit/pre-commit>`_, `uv
<https://github.com/astral-sh/uv>`_, and `huggingface_hub <https://github.com/huggingface/huggingface_hub>`_ each
arrived at this shape on their own, because a silent wait reads as a hang.

Exactly one ``release()`` balances this, whichever branch ran: a failed ``acquire`` rolls the lock counter back, so the
probe leaves nothing behind to unwind. Do not reach for ``release(force=True)`` here, because it would discard a
reentrant hold your caller still depends on.

*******************************
 Report progress while waiting
*******************************

Probing tells the user *once* that you are waiting. For a wait that can run for minutes, keep telling them. Wrap the
acquire in a loop with a short inner timeout and an overall deadline:

.. code-block:: python

    import logging
    import time

    from filelock import FileLock, Timeout

    lock = FileLock("cache/.locks/models/bert-base/a1b2c3.lock")
    deadline = time.monotonic() + 300

    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise Timeout(lock.lock_file)
        try:
            lock.acquire(timeout=min(10, remaining))
        except Timeout:
            logging.info("still waiting on %s, %.0fs left", lock.lock_file, remaining)
        else:
            break

    try:
        ...  # critical section
    finally:
        lock.release()

The inner timeout controls how often you speak; the deadline controls when you give up. Keeping them separate means a
chatty log never shortens the wait, and a long wait never goes quiet. ``huggingface_hub`` runs this pattern in its
``WeakFileLock`` to keep a stalled model download from looking like a crashed one.

.. mermaid::

    sequenceDiagram
        box rgba(21, 101, 192, 0.16) This process
            participant C as Caller
        end
        box rgba(230, 81, 0, 0.16) Coordination
            participant L as File Lock
        end
        box rgba(69, 39, 160, 0.16) Peer
            participant H as Holder
        end
        activate H
        activate L
        Note over H: holds the lock
        loop until deadline
            C->>L: acquire(timeout=10)
            Note over L: 10s elapse, still held
            L-->>C: raise Timeout
            C->>C: log "still waiting"
        end
        H->>L: release()
        deactivate L
        deactivate H
        C->>L: acquire(timeout=10)
        L-->>C: acquired
        activate C
        activate L
        Note over C: critical section
        C->>L: release()
        deactivate L
        deactivate C

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

``acquire`` and ``release`` always run on the event loop that awaits them. The ``loop`` argument only matters when the
lock is garbage collected while still held: the finalizer schedules the release on that loop if no loop runs in the
collecting thread. Without any loop, it releases a built-in backend synchronously.

Async locks default to ``thread_local=False`` (unlike sync locks which default to ``True``) because the
acquiring and releasing threads may differ when using an executor.

Available async lock classes:

- :class:`AsyncFileLock <filelock.AsyncFileLock>`, platform-aware (recommended).
- :class:`AsyncSoftFileLock <filelock.AsyncSoftFileLock>`.
- :class:`AsyncStrictSoftFileLock <filelock.AsyncStrictSoftFileLock>`.
- :class:`AsyncSoftFileLease <filelock.AsyncSoftFileLease>`.
- :class:`AsyncUnixFileLock <filelock.AsyncUnixFileLock>`.
- :class:`AsyncWindowsFileLock <filelock.AsyncWindowsFileLock>`.
- :class:`AsyncReadWriteLock <filelock.AsyncReadWriteLock>`.
- :class:`AsyncSoftReadWriteLock <filelock.AsyncSoftReadWriteLock>`.

*********************************
 Use locks with multiple threads
*********************************

By default, locks are thread-local. Each thread keeps its own lock state, so nested acquisitions from the same thread
don't block, while two threads exclude each other just as two processes do:

.. code-block:: python

    from filelock import FileLock
    import threading

    lock = FileLock("work.lock")  # thread_local=True by default


    def worker():
        with lock:
            print(f"{threading.current_thread().name} has the lock")


    # The worker thread and the main thread take turns: each waits while the other holds the lock
    threading.Thread(target=worker).start()
    worker()  # Main thread

If you need one hold shared by every thread, set ``thread_local=False``. A second thread then enters without waiting
and only bumps the shared counter, so the lock no longer keeps threads apart:

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

Parameters are frozen when the singleton is first created. Requesting the same path with different parameters raises
``ValueError``:

.. code-block:: python

    lock1 = FileLock("other.lock", is_singleton=True, timeout=10)  # freezes timeout=10
    lock2 = FileLock("other.lock", is_singleton=True, timeout=5)  # ValueError!

Equivalent relative and absolute paths can return the same singleton. If its original spelling resolves to a different
file after a directory change, an unheld shared singleton raises ``RuntimeError`` rather than locking that other file.
Construct a new lock from the absolute path after changing directories. A singleton used with only one spelling still
resolves its relative path when acquisition starts.

*****************************************
 Use shared read / exclusive write locks
*****************************************

When you have many readers and occasional writers, use :class:`ReadWriteLock <filelock.ReadWriteLock>` to allow readers
to proceed concurrently. ``ReadWriteLock`` is backed by SQLite; a ``.db`` extension is a convention rather than a
requirement. Use a local filesystem the active SQLite VFS supports and a database file of its own. A database in WAL
mode or with unsupported SQLite file-format versions raises :class:`ValueError`: neither provides a trusted exclusive
write hold.

SQLite loads the schema before it can honor the acquire timeout. filelock limits each row to 1,024 bytes during this
load and interrupts long-running schema queries. A lock database that exceeds either limit raises :class:`ValueError`.

On POSIX, filelock opens the database directly when its directory belongs to the locking account or root, its group
and others cannot write there, and none of SQLite's side files (``-journal``, ``-wal`` or ``-shm``) exist.

Otherwise, filelock creates a private directory under ``.filelock-*`` and opens a hard link to the database there.
This keeps SQLite's side files out of the shared directory. The locking account needs write access to the parent and a
filesystem that supports hard links. If inherited permissions expose the private directory, filelock sets mode 0700 and
checks it. If other accounts still have access or the filesystem refuses the mode change, filelock raises :class:`PermissionError`.

Each process checks shared directories for links left by dead holders. It checks a 50 ms deadline between entries.
If time runs out, connections to that database skip cleanup for one minute. The next connection then continues from
the last entry. Each process keeps up to 16 unfinished scans. An evicted scan starts over when its database is next
eligible. After visiting 1,024 databases, filelock can forget an older scan and check that database again. A leftover
link does not keep the lock held.

On POSIX, SQLite refuses to create a database if its private link disappears before the connection opens. On Linux,
when procfs cannot establish the process's PID namespace, filelock leaves private links from other processes in place.
Upgrade all processes that share the database together. Older releases can create a separate database if their private
link disappears. Stop all users of the database before removing any stale ``.filelock-*`` directories by hand.

Both routes assume that no process of another account can replace the database directory or any directory above it.
That holds when every directory above belongs to the locking account or root and is either not writable by others or
has the sticky bit (a directory flag that lets only a file's owner rename or remove it).

Keep the database under directories trusted by the locking account. If another account can replace a directory above
the database, it can change which files SQLite sees between the identity check and the first SQL statement. SQLite
can then delete a journal or WAL in the replacement directory. With a nonempty database, SQLite can replay the journal
into that database and unlink the file named by the journal.

On Windows the lock makes the same choice from the directory's access control list (ACL). It connects by the database
name only when the directory's owner and every entry that allows adding files, adding subdirectories or deleting entries
name the locking account, ``SYSTEM`` or ``Administrators``. Otherwise it links the database into a directory created
with an ACL that grants only those accounts, and SQLite opens the link. The filesystem must support hard links, or the
lock raises :class:`OSError`. Where it does not, as on FAT or some network shares, keep the database in a directory
other accounts cannot create files in. The per-user temporary directory and ``%LOCALAPPDATA%`` qualify,
``C:\Windows\Temp`` and ``C:\Users\Public`` do not.

.. code-block:: python

    from filelock import ReadWriteLock

    rw = ReadWriteLock("data.db")

    # Multiple processes can read simultaneously
    with rw.read_lock():
        data = get_shared_data()

    # Only one process can write at a time
    with rw.write_lock():
        update_shared_data()

A write lock raises :class:`ValueError` if SQLite opens the database read-only after the header check. A writer
checks that its transaction can write before entering the critical section.

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

A write lock belongs to the thread that acquired it, so ``release()`` from another thread raises ``RuntimeError``.
Threads that share a read lock each own their levels, and ``release()`` from a thread holding none raises
``RuntimeError`` too. ``release(force=True)`` and ``close()`` drop the lock from any thread.

One instance coordinates processes, not threads: a thread that asks for the opposite mode of another thread's hold gets
``RuntimeError`` instead of waiting. Threads that must wait for each other need their own hold, so give each thread its
own instance:

.. code-block:: python

    rw = ReadWriteLock("data.db", is_singleton=False)  # one per thread

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

A ``timeout`` of ``-1`` or ``math.inf`` waits up to SQLite's busy-timeout cap of about 23 days, then raises
:class:`~filelock.Timeout`. A blocking acquire raises :class:`ValueError` for ``nan`` or any other negative ``timeout``,
on :class:`SoftReadWriteLock <filelock.SoftReadWriteLock>` too.

A synchronous SQLite wait returns to Python between busy retries so a single-threaded process can handle signals.
Once its writer is pending, later readers wait for it. If a handler raises, the interrupted writer gives up its place.
If the handler waits for the same database through a second lock instance, the pending writer cannot proceed until the
handler returns. Defer that acquisition until after the handler returns, or use ``blocking=False`` and handle
:class:`~filelock.Timeout`.

On systems with fork support, a multithreaded process releases the connection between attempts to let other threads
fork. A reader can enter between those attempts.

Leave the database file in place while any process may hold the lock. The lock lives on the file, not on its name, so
after an unlink or a replace the next opener creates a fresh file and acquires at once while the old holder keeps its
lock on the orphan. On POSIX an acquisition that sees the path replaced while it waited raises :class:`OSError` instead
of taking a lock no later opener can see.

On POSIX, create :class:`~filelock.ReadWriteLock` and :class:`~filelock.AsyncReadWriteLock` in the main interpreter.
Closing a database file descriptor in a subinterpreter can release another interpreter's SQLite lock. Construction in
a subinterpreter raises :class:`RuntimeError` when filelock can identify it. CPython 3.12 and later expose this
directly. Embedded CPython 3.11 needs ``_xxsubinterpreters`` or ``ctypes`` with access to the interpreter C API.

Run the lock operation in the main interpreter or a separate process. :class:`~filelock.SoftReadWriteLock` is an
alternative when every participant uses it and the interpreter permits its daemon heartbeat thread.

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
descriptor and its SQLite memory per active lock. CPython 3.11 need an extra raw reference to stop their base
deallocator from closing the handle; later versions suppress finalization through the connection subclass. Both paths
avoid the ``sqlite3_close()`` call that SQLite forbids after a fork.

filelock normally rejects ``fork()`` from a callback that runs during a SQLite operation. The guard is an audit hook,
and CPython 3.11 fail a concurrent ``sys.settrace`` call while any Python audit hook runs, which coverage and
debuggers make on every thread start, so filelock skips it there. Without the guard, whether skipped or blocked by
another audit hook, a child created at this boundary exits with status 70 before it can touch the inherited connection;
the parent operation continues.

A native or signal callback that forks while its thread is executing inside SQLite can resume in inherited C state;
filelock cannot make that operation safe.

.. _fork guidance: https://www.sqlite.org/howtocorrupt.html#_carrying_an_open_database_connection_across_a_fork_

********************************************
 Use read/write locks on shared filesystems
********************************************

:class:`ReadWriteLock <filelock.ReadWriteLock>` is SQLite-backed and requires a local filesystem supported by the active
SQLite VFS. On a shared filesystem, use :class:`SoftReadWriteLock <filelock.SoftReadWriteLock>` only after verifying
exclusive creation, no-replace hard links, unlink, and cache visibility across participating hosts. A peer evicts a
holder whose heartbeat has stopped for ``stale_threshold``, even if the old holder later resumes; ``on_compromise``
tells that holder.

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
        heartbeat_interval=30,   # how often the heartbeat rewrites the holder's nonce
        stale_threshold=90,      # evict a holder after this many seconds without a change
        poll_interval=0.25,      # how long to sleep between acquire retries
    )

Pick ``stale_threshold`` larger than any realistic process or filesystem pause. ``heartbeat_interval`` should be
roughly ``stale_threshold / 3``; ``stale_threshold`` defaults to exactly that and must strictly exceed the interval.
Lower ``poll_interval`` reduces acquisition latency at the cost of more filesystem metadata calls. Each holder writes
its ``stale_threshold`` into its record, and a peer evicts it only once the longer of its own threshold and that one has
passed, so a peer tuned tighter does not evict a live holder that heartbeats on a slower schedule. No clock is compared
across hosts, so clock skew cannot evict a live holder. Fence protected writes with ``generation`` if an
expired holder can resume:

.. code-block:: python

    def stop_working(compromise):
        print(f"lost the lock: {compromise.reason}")

    rw = SoftReadWriteLock("/shared/nfs/data.lock", on_compromise=stop_working)

    with rw.write_lock():
        # A store that rejects a lower generation than the highest it accepted refuses an evicted holder that resumes.
        store.write(payload, fence=rw.generation)

As with ``ReadWriteLock``, ``release()`` of a write lock from another thread raises ``RuntimeError``, and so does
``release()`` from a thread that holds no level of a shared read lock. ``on_compromise`` runs on the hold's own heartbeat
thread, so it may release, and ``release(force=True)`` and ``close()`` work from any thread.
If peers keep winning generation updates until this holder reaches ``stale_threshold``, release raises
:class:`SoftFileLockProtocolError <filelock.SoftFileLockProtocolError>`. Its holder record is removed, but the generation
log may still name it; a later contender waits for stale-holder eviction.

``timeout`` and ``blocking`` set instance-wide defaults that each acquisition inherits. Passing ``None`` per call means
"use the instance default", which is not the same as ``-1``:

.. code-block:: python

    rw = SoftReadWriteLock("/shared/nfs/data.lock", timeout=30, blocking=True)

    with rw.read_lock():  # inherits timeout=30
        pass

    with rw.read_lock(timeout=-1):  # blocks forever, overriding the instance default
        pass

    with rw.read_lock(blocking=False):  # one attempt; ignores timeout entirely
        pass

``blocking=False`` makes one attempt without sleeping. A writer that gives up while waiting for readers commits itself
out of the snapshot, so its own timeout does not leave readers blocked. The next contender, on whichever host, evicts
a holder that died on another host once ``stale_threshold`` has passed. Filesystem calls on an unresponsive
network mount can still outlast the acquisition timeout.

Each generation snapshot is flushed to stable storage before publication. If a snapshot is corrupt, acquisition raises
:class:`SoftFileLockProtocolError <filelock.SoftFileLockProtocolError>` with its name. Skipping a corrupt snapshot
could admit two writers.

To recover, rename the whole ``<path>.rw`` directory aside, then delete it. Renaming removes the tree in one step.
A recursive delete on the live path can leave other participants reading a partial log. Wait until all holders finish
if possible. A holder that loses the tree learns of the loss through ``on_compromise`` at its next heartbeat.

A waiter also recreates a missing ``gen/`` directory when it retries publication. The first snapshot in the new log has
generation 1. A waiter may enter at a later generation. Reset the high-water mark of any resource you fence with
``generation``.

.. warning::

   ``SoftReadWriteLock`` and ``ReadWriteLock`` are singletons by default. A second construction for the same path
   raises ``ValueError`` if ``timeout`` or ``blocking`` differ, and ``SoftReadWriteLock`` also raises when it passes a
   different ``on_compromise``. ``heartbeat_interval``, ``stale_threshold``, and ``poll_interval`` keep the first call's
   tuning on a cache hit. ``SoftReadWriteLock`` still validates these intervals before returning the cached instance.
   Configure a path in one place.

:meth:`get_lock() <filelock.SoftReadWriteLock.get_lock>` is sugar for the same singleton lookup, spelling the intent
out at the call site. ``ReadWriteLock`` has the same classmethod. It offers nothing the constructor does not:

.. code-block:: python

    assert SoftReadWriteLock.get_lock("/shared/nfs/data.lock") is SoftReadWriteLock("/shared/nfs/data.lock")

Writer acquisition is writer-preferring: a writer enters the next snapshot as soon as no live writer is named (which
blocks any new reader), then waits for the readers that snapshot names to leave. This rules out writer starvation under
read-heavy workloads. See :doc:`concepts` for the full model.

**Fork caveat.** A process that forks while holding a ``SoftReadWriteLock`` loses the lock in the child. filelock marks
the inherited instance fork-invalidated; ``release()`` on it becomes a no-op, and the child must construct a fresh
``SoftReadWriteLock(path)`` before acquiring. This follows the invalidation approach used by PyMongo's connection
pools.

**Trust boundary.** The class coordinates cooperating processes at one UID. Mode bits (``0o600`` / ``0o700``) keep other
UIDs out; they do not make a same-UID co-tenant safe, since it owns the records and can rewrite or delete them directly.
A filesystem that delays a heartbeat's visibility past ``stale_threshold`` can also trigger expiry while a holder
remains active. See :ref:`concepts:The same-UID boundary` for the full contract.

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

Each ``asyncio`` task owns its own hold, whichever executor thread runs the SQLite call. Tasks share a read lock, and a
task asking for the write lock waits until the other holders release, with waiting writers ahead of new readers. A task
that acquires again nests into its hold. Only the task that acquired may release; a release from another task raises
:class:`RuntimeError`. :class:`AsyncSoftReadWriteLock <filelock.AsyncSoftReadWriteLock>` follows the same rules.

.. warning::

    On Python 3.11, ``asyncio.wait_for(rw.acquire_write(), 5)`` runs the acquire in a child task, which then
    owns the hold, so releasing it from the caller raises. Bound the wait with the lock's own ``timeout`` or with
    ``asyncio.timeout()`` instead.

You can pass a custom executor, and an explicit event loop:

.. code-block:: python

    from concurrent.futures import ThreadPoolExecutor

    executor = ThreadPoolExecutor(max_workers=2)
    rw = AsyncReadWriteLock("data.db", executor=executor, loop=loop)

By default, ``AsyncReadWriteLock`` creates a dedicated single-worker pool because SQLite pins a connection to one
thread. ``await rw.close()`` waits for its worker to exit, even after the event loop's default executor has shut down.
If the task awaiting ``close()`` is canceled, await ``rw.close()`` again to finish the same shutdown.
If you pass your own executor, shut it down yourself. With ``loop=None``, each call uses the running loop:

.. code-block:: python

    rw = AsyncReadWriteLock("data.db")  # owns a private single-worker executor
    try:
        async with rw.read_lock():
            data = await get_shared_data()
    finally:
        await rw.close()  # shuts down the executor it created

With the default ``is_singleton=True`` every wrapper on a path shares one lock, so ``close()`` ends only the holds of
tasks that acquired through that wrapper. Tasks holding through another wrapper, and synchronous ``ReadWriteLock`` users
of the same path, keep their locks. An acquire waiting for another async wrapper's hold wakes and raises the closed
error.

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

By default, each wrapper starts a worker thread on its first backend call. Call ``await rw.close()`` when you finish
with the wrapper to shut down its executor. If you pass an executor, reserve capacity for releases while acquires are
waiting, and shut it down yourself.

When async wrappers share a lock instance in one process, they serve queued writers in arrival order before admitting
new readers. If the first writer's event loop pauses, later writers and new readers wait until that loop runs again.

Closing a wrapper wakes tasks waiting through it and interrupts its own contended backend acquire on the next poll.
A singleton wrapper can close while another wrapper waits to acquire the backend. If an acquire obtains the lock while
its wrapper is closing, it waits for the outcome. A successful close releases the hold. If close fails with the lock
still held, the acquiring task keeps it and can release it.

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

Where available, the marker stores a process start token to guard against PID recycling: Linux uses the
``/proc/<pid>/stat`` start time folded with the boot id, macOS reads it through ``sysctl``, and Windows uses the
``GetProcessTimes`` creation time. Malformed records follow a different rule: a waiter may evict them once it has seen
them unchanged for two seconds. An empty marker, which is all a ``SoftFileLock`` before 3.22 writes, is never evicted
this way; remove a leftover one with ``break_lock()``.
That recovery path is not fail closed.

If a POSIX filesystem fills during acquisition, cleanup tries to leave a one-byte marker when it cannot remove the
failed record. Free space so the next caller can reclaim the marker on filesystems that support atomic exchange.

A contender breaks a stale marker without the lock path ever standing empty. Where it cannot (NFS, SMB and platforms
without an atomic exchange, other than Windows) it leaves the marker in place; see :ref:`concepts:How does stale lock
detection work across platforms?` for the details.

********************************
 Use fail-closed soft locks
********************************

Use :class:`StrictSoftFileLock <filelock.StrictSoftFileLock>` when an ambiguous claim must block access. It publishes
complete owner-specific records through no-replace hard links and removes only its own claim. A process pause during
publication cannot expose a partial public record, and release cannot unlink a successor's claim.

.. versionadded:: 3.30.0

.. code-block:: python

    from filelock import StrictSoftFileLock

    lock = StrictSoftFileLock("work.lock")

    with lock:
        update_shared_resource()

The lock requires coherent directory reads and atomic hard links. Use it on a local filesystem whose contract provides
both operations. If hard-link publication is unavailable, acquisition raises
:class:`SoftFileLockProtocolError <filelock.SoftFileLockProtocolError>`. NFS and SMB mounts have no strict guarantee
without a contract test for the specific client, server, and mount options.

Strict mode leaves ``work.lock`` as a permanent sentinel and stores claims in ``work.lock.filelock/claims``. Every
strict participant must use ``StrictSoftFileLock`` or ``AsyncStrictSoftFileLock`` from filelock 3.30.0 or newer. During
migration, a legacy ``SoftFileLock`` holder blocks the first strict acquisition until it releases. The first strict
acquisition activates the path; filelock 3.20.0 and other legacy clients then time out before entry, including between
strict holds. Age expiry, ``break_lock()``, or manual sentinel deletion voids this guarantee. See the `filelock 3.20.0
release <https://github.com/tox-dev/filelock/releases/tag/3.20.0>`_ for the oldest migration client tested by filelock.

A soft-lock owner's marker at ``work.lock`` (from ``SoftFileLock``, ``MarkerSoftFileLock`` or ``SoftFileLease``) blocks
strict acquisition like any holder, without appearing in ``lock.claims``; :attr:`lock_path_occupant
<filelock.StrictSoftFileLock.lock_path_occupant>` describes it, with its PID and host when the marker records them. A
file strict cannot read blocks the same way, because Windows refuses to read a marker its holder keeps open. A file it
reads but cannot parse, such as the empty marker filelock before 3.22 wrote or an empty ``UnixFileLock`` lock file,
blocks while it changes and raises :class:`SoftFileLockProtocolError <filelock.SoftFileLockProtocolError>` once it stays
unchanged for two seconds, the grace a soft contender waits before evicting a malformed marker other than an empty one.

A marker whose owner on this host has exited raises the same error naming that owner, and a directory, symlink, or other
node no lock writes raises it after half a second. Strict mode never removes what it found. A crashed owner on another
host, or one that left no PID, cannot be told apart from a live one, so its marker blocks until an operator who has
confirmed that owner is gone deletes ``work.lock``.

A crash can leave an intent, a held claim, or both claims for one token. Strict mode treats each claim as live because
PID and clock checks cannot prove that another host or a recycled process has stopped using the resource. Inspect the
parsed claims before recovery:

.. code-block:: python

    from filelock import StrictSoftFileLock

    lock = StrictSoftFileLock("work.lock")

    for claim in lock.claims:
        print(claim.name, claim.state, claim.token, claim.hostname, claim.pid)

After an operator verifies that the named owner no longer uses the protected resource, remove every claim for its
token:

.. code-block:: python

    crashed_token = "0123456789abcdef0123456789abcdef"
    for claim in lock.claims:
        if claim.token == crashed_token:
            lock.force_break(claim.name)

``force_break()`` can admit a contender while the removed claim's owner still runs. It validates a single basename and
uses directory-relative deletion on platforms that support it. Unknown record versions, malformed records, symlinks,
and unreadable claims raise :class:`SoftFileLockProtocolError <filelock.SoftFileLockProtocolError>`; the exception's
``claim_name`` identifies the entry that needs inspection.

An acquisition that fails after publishing a claim removes that claim before it raises, so a failed attempt does not
block other contenders. If removing it fails too, :attr:`is_locked <filelock.BaseFileLock.is_locked>` stays ``False``
and the next ``acquire()`` on the same instance retries the removal before publishing again, raising until it succeeds.
Garbage collecting the instance retries it as well.

Each publication attempt uses a fresh random private-record name. On POSIX, a private record with another hard link is
removed immediately; an unpublished record abandoned by a crash is removed after a two-second grace period. If a live
publisher is paused past that grace period, it backs off and retries instead of entering without a public claim.

On Windows, acquisition leaves abandoned private records in place. Before publication, more than 16,380 existing
directory entries trigger a protocol error that names the directory. The contender reserves the remaining four slots
for its own records and a clock probe. After publication, growth beyond 16,384 entries makes the attempt withdraw.
Only public claims count toward the separate 1,024-claim limit.

Stop all processes using this lock before cleaning up. Next to the lock file, remove its abandoned
``.<lock-name>.private-v1-*.tmp`` sentinel records. Under ``<lock-name>.filelock/claims/``, remove abandoned
``.*.private-v1-*.tmp`` and ``.probe-v1-*.tmp`` records. Leave the permanent lock-file sentinel in place. For public
``*.claim`` records, verify the owner has stopped and use ``force_break()`` as shown above.

A directory, symlink, or other non-regular node at a private-record name is protocol damage and fails closed without
following or removing it.

Async code uses the same on-disk protocol:

.. code-block:: python

    from filelock import AsyncStrictSoftFileLock

    lock = AsyncStrictSoftFileLock("work.lock")

    async with lock:
        await update_shared_resource()

``StrictSoftFileLock`` rejects ``on_acquired`` because its borrowed descriptor is the permanent protocol sentinel; a
callback could corrupt that sentinel. ``lock.claims`` provides owner metadata without exposing a writable descriptor.

The publication sequence follows the private-claim pattern in `flufl.lock
<https://gitlab.com/warsaw/flufl.lock/-/blob/main/src/flufl/lock/_lockfile.py>`_ and the pre/post claim checks in
`restic <https://github.com/restic/restic/blob/master/internal/repository/lock_file.go>`_.
Issue :issue:`637` records the race analysis and verification requirements.

****************************************************
 Limit the wait for strict lock release on Windows
****************************************************

On Windows, ``StrictSoftFileLock.release()`` waits for the filesystem to withdraw the public claims and close their
handles. To limit how long the calling thread waits, use :meth:`~filelock.StrictSoftFileLock.release_with_timeout`.
Like ``release()``, it drops one acquisition. Only the final release starts a worker on Windows. On POSIX, the same
method calls ``release()`` synchronously, so filesystem I/O can outlast ``timeout``.

.. code-block:: python

    from filelock import ReleasePendingError, StrictSoftFileLock

    lock = StrictSoftFileLock("work.lock")
    lock.acquire()
    try:
        update_shared_resource()
    finally:
        try:
            lock.release_with_timeout(0.1)
        except ReleasePendingError as pending:
            report_delayed_release()
            pending.wait()

:class:`~filelock.ReleasePendingError` identifies the release still in progress. Call
:meth:`pending.wait() <filelock.ReleasePendingError.wait>` from any thread to get its result, even after another holder
acquires the same lock instance. For another bounded wait, use ``pending.wait(timeout=0.1)``. If that deadline passes,
it raises another ``ReleasePendingError`` for the same operation. Calling ``lock.release()`` acts on the current hold,
which may already belong to another caller on a shared instance.

If withdrawal or handle closure fails, ``pending.wait()`` raises the original filesystem error. If withdrawal failed and
the lock is still held, retry release on the original lock. With the default thread-local ownership, the acquiring
thread must make that retry. A plain :exc:`TimeoutError` while waiting for another thread to finish a lock transition
means release could not start. Interrupting a wait leaves the worker running. Use the receipt to wait again.

Finish using the protected resource before starting release. The owner's ``is_locked`` stays true while the worker runs.
Once Windows withdraws the public claims, another process can acquire while the worker finishes closing the handles.

An ``acquire()`` that encounters a pending release on the same hold waits according to its own ``timeout`` and
``cancel_check``. A nonblocking attempt, an expired deadline, or cancellation raises :class:`~filelock.Timeout`.
Use the pending release's ``wait()`` to retrieve any error from its worker.

The interpreter does not join the daemon worker at shutdown. The release deadline applies to the caller's wait.
It gives no bound on Windows process exit. If the process exits before withdrawal, public claims can
remain. Verify that the owner has stopped using the resource before removing its claims with ``force_break()``. See
:ref:`how-to:Use fail-closed soft locks`.

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

***********************************
 Inspect a protocol 2 owner record
***********************************

``SoftFileLock`` publishes a *protocol 1* marker: a PID and a hostname. :class:`SoftFileLease
<filelock.SoftFileLease>` publishes a *protocol 2* marker, which also names the contract its holder acquired under. It
reads that marker back through :class:`MarkerSoftFileLock <filelock.MarkerSoftFileLock>`, its base class, so ``owner``
returns an :class:`OwnerRecord <filelock.OwnerRecord>`:

.. code-block:: python

    from filelock import SoftFileLease

    lease = SoftFileLease("work.lock", lease_duration=30)

    if (owner := lease.owner) is None:
        print("no marker, or its record is malformed or protocol 1")
    else:
        print(owner.pid, repr(owner.hostname))
        print(owner.mode)            # "lease", or "unknown" for a contract this version cannot interpret
        print(owner.token)           # claim identity
        print(owner.lease_duration)  # 30.0
        print(owner.start)           # process start token, or None where unavailable

Every record holds ``socket.gethostname()`` with each byte outside printable non-space ASCII, and each ``?``,
escaped as ``?<hex>``, since a marker line cannot carry a space, a newline, or a byte no codec encodes. A
conventional hostname reaches the record unchanged, so an escape names a host whose kernel hostname is not one. On
Linux, a process outside the host's initial PID namespace appends ``?pidns-<inode>``, so a container's records never
match a sibling container's that shares its hostname; see :ref:`concepts:How does stale lock detection work across
platforms?`.

Use ``repr(owner.hostname)`` when printing a marker's raw hostname. :class:`~filelock.Timeout` and
:attr:`~filelock.StrictSoftFileLock.lock_path_occupant` escape it for display.

:class:`StrictSoftFileLock <filelock.StrictSoftFileLock>` does **not** publish this record and has no ``owner``: it
derives from :class:`BaseFileLock <filelock.BaseFileLock>`, keeps a permanent sentinel at the lock path, and stores one
record per owner under ``work.lock.filelock/claims``. Read those through
:attr:`claims <filelock.StrictSoftFileLock.claims>` instead, as :ref:`how-to:Use fail-closed soft locks` shows.

``owner`` reads the marker on disk each time, so it reports whoever currently holds the path, not necessarily this
process. To ask specifically about this process, use ``is_lock_held_by_us``, which compares the PID, the hostname
and, when the marker records one, the process start token:

.. code-block:: python

    if lease.is_lock_held_by_us:
        print("this process wrote the marker")

``owner.mode`` reads ``"unknown"`` when the marker names a contract this version does not implement, which is how a
record written by a newer filelock reads to an older one. A lease never reclaims such a marker by age: only a peer that
published a lease agreed to be superseded by one. Treat ``"unknown"`` as "held by something I should not touch".

Use these records to build recovery tooling, and keep two limits in mind. ``None`` is ambiguous by design: a missing
marker, a malformed one, and a protocol 1 one all read the same, because none of them names an owner this contract can
trust. And a record is a *report*, not a lock: reading one proves nothing about the next instant. Never gate entry on
what ``owner`` returned; acquire the lock.

:meth:`force_break() <filelock.MarkerSoftFileLock.force_break>` removes the marker whoever holds it:

.. code-block:: python

    lease.force_break()  # voids mutual exclusion; the old holder keeps running

Reserve it for an operator who has confirmed the named owner is gone. ``StrictSoftFileLock.force_break()`` is a
different call: it removes one claim by name rather than a single marker.

*********************************************
 Type options passed through a lock subclass
*********************************************

Every keyword filelock's metaclass forwards to a lock is declared in :class:`LockOptions <filelock.LockOptions>`, a
``TypedDict``. A subclass that adds its own options can use it to type the rest, instead of widening them to
``**kwargs: Any``:

.. code-block:: python

    from typing import Unpack

    from filelock import FileLock, LockOptions


    class CountedFileLock(FileLock):
        def __init__(self, lock_file: str, *, uses: int = 0, **kwargs: Unpack[LockOptions]) -> None:
            self.uses = uses
            super().__init__(lock_file, **kwargs)

A type checker now rejects ``CountedFileLock("x.lock", timeuot=5)`` at the call site rather than letting the typo reach
``__init__``. filelock uses ``LockOptions`` internally for the same reason, and virtualenv's ``util/lock.py`` wraps
``FileLock`` in a counted subclass much like this one.

*****************
 Control logging
*****************

Every message goes to the ``filelock`` logger. All of them are ``DEBUG``, except one ``WARNING`` that
:class:`ReadWriteLock <filelock.ReadWriteLock>` emits when a requested timeout exceeds what SQLite's ``busy_timeout``
accepts. Control logging via Python's standard library:

.. code-block:: python

    import logging

    # Hide filelock debug messages; the ReadWriteLock timeout warning still gets through
    logging.getLogger("filelock").setLevel(logging.INFO)

    # Or show all messages
    logging.getLogger("filelock").setLevel(logging.DEBUG)

    # Silence filelock entirely, warning included
    logging.getLogger("filelock").setLevel(logging.ERROR)

    # Configure a handler to see them
    handler = logging.StreamHandler()
    logging.getLogger("filelock").addHandler(handler)

*******************************
 Choose a soft-lock contract
*******************************

:class:`StrictSoftFileLock <filelock.StrictSoftFileLock>` never reclaims a marker. A malformed record, an owner on
another host, a dead PID and an old marker all read as held, so acquisition waits rather than overlap a holder that may
still be alive. A crashed holder leaves a marker no contender removes; clear it with
:meth:`force_break <filelock.StrictSoftFileLock.force_break>`, which voids mutual exclusion for whoever is still
running.

The heartbeat refreshes the lease marker while work runs. A peer can take the marker after it sees no refresh for
``lease_duration`` seconds on its own monotonic clock. A peer on the same host can reclaim a lease or
``MarkerSoftFileLock`` immediately if the owner has died.

The heartbeat calls ``on_compromise`` when it detects a lost claim. Release can also record the loss in
:attr:`compromise <filelock.SoftFileLease.compromise>` after work ends. An expired holder can continue writing.
The :attr:`token <filelock.SoftFileLease.token>` identifies the claim. To reject stale writes, the resource must
atomically check a generation it controls.

.. code-block:: python

    from filelock import SoftFileLease, StrictSoftFileLock

    with StrictSoftFileLock("strict.lock", timeout=30):
        pass  # no peer enters while this holder lives

    def stop_working(compromise):
        print("lost the claim:", compromise.reason)

    with SoftFileLease("lease.lock", lease_duration=60, on_compromise=stop_working):
        pass  # a peer may enter 60s after the last refresh

Every contender for a path must agree on ``lease_duration``; one that disagrees raises
:class:`LeaseSettingsMismatch <filelock.LeaseSettingsMismatch>` instead of applying its own expiry to a peer that never
agreed to it. Native locks take no lease settings, because pathname age cannot revoke a kernel lock on an inode; they
warn and ignore a ``lifetime`` rather than raising. Async callers use ``AsyncStrictSoftFileLock`` and
``AsyncSoftFileLease``.

Tune the refresh with ``heartbeat_interval``, which defaults to ``lease_duration / 3`` and must satisfy
``0 < heartbeat_interval < lease_duration`` and stay within :data:`threading.TIMEOUT_MAX` (anything else raises
:class:`ValueError`):

.. code-block:: python

    lease = SoftFileLease("work.lock", lease_duration=60, heartbeat_interval=20)

Unlike ``lease_duration``, this one is local: it never reaches the marker, so peers on a path may each choose their own
without raising ``LeaseSettingsMismatch``. It buys margin rather than time. A refresh that fails transiently is retried,
and the lease reports ``refresh-failed`` only once ``lease_duration - heartbeat_interval`` has passed without a
success, which is deliberately *before* a peer may legally take the claim. The default leaves room for two missed
refreshes; an interval close to ``lease_duration`` collapses that margin to nearly nothing, while a small one adds
metadata traffic on the network filesystem these leases usually live on. ``release()`` also joins the heartbeat thread
with a timeout of one interval, so a large value lengthens a worst-case release. The heartbeat holds the lease only
weakly, so a lease dropped while held is released when it is garbage collected or the interpreter exits, as every other
lock is.

Windows refuses to rename or delete a file another process holds open, so a peer takes an expired claim there only once
the previous holder's process exits and its handle closes. A holder that keeps running but stops refreshing keeps its
marker on Windows, while Unix lets a peer reclaim it after ``lease_duration``.

**Do not mix contracts on one path.** The two classes publish different records, and only one of them is safe to leave
in front of a legacy contender. ``SoftFileLease`` writes a protocol 2 marker, which
:class:`SoftFileLock <filelock.SoftFileLock>` reads as malformed and evicts once past its grace period, deleting a live
lease. ``StrictSoftFileLock`` instead leaves a sentinel that a current ``SoftFileLock`` recognizes and preserves, so it
blocks rather than breaking in. Only these combinations hold:

.. list-table:: Mutual exclusion between contenders
    :header-rows: 1

    - - Contenders on one path
      - Holds?
    - - ``StrictSoftFileLock`` with ``StrictSoftFileLock``
      - Yes.
    - - ``SoftFileLease`` with ``SoftFileLease``, same ``lease_duration``
      - Until a claim expires; then the old holder overlaps its successor.
    - - ``SoftFileLock`` with ``StrictSoftFileLock``
      - Yes, from filelock 3.30.0 on: the strict sentinel is recognized, so the legacy contender waits. A pre-3.30
        ``SoftFileLock`` misreads the sentinel and can break in. On POSIX, strict acquisition refuses a sentinel owned
        by another account under a sticky parent, since its owner could remove it during the hold.
    - - ``SoftFileLock`` with ``SoftFileLease``
      - No. The legacy contender reads the protocol 2 marker as malformed and evicts it.
    - - ``StrictSoftFileLock`` with ``SoftFileLease``
      - The lease waits out a strict holder, but a strict contender never reclaims an expired lease.

***********************************
 Configure legacy age-based expiry
***********************************

The ``lifetime`` value sets the age at which a :class:`SoftFileLock <filelock.SoftFileLock>` waiter may remove a marker.
A ``UnixFileLock`` that has already fallen back after ``ENOSYS`` also accepts a ``lifetime`` assignment for that path.
A waiter may enter after that age even while the previous holder continues its protected operation. The age is the
waiter's wall clock minus the marker's modification time, which the writer's or the file server's clock stamped, so
every host must keep its clock in sync: a waiter whose clock runs ``lifetime`` ahead removes a marker written a moment
ago. ``SoftFileLease`` compares no clocks across hosts.

.. code-block:: python

    from filelock import SoftFileLock

    # A waiter may remove this marker after one hour.
    lock = SoftFileLock("work.lock", lifetime=3600)

    with lock:
        # This operation can overlap a successor after the marker expires.
        pass

Constructing or assigning a non-``None`` value emits
:class:`SoftFileLockLifetimeWarning <filelock.SoftFileLockLifetimeWarning>`, a :class:`FutureWarning` that Python shows
by default; silence it with ``-W ignore::filelock.SoftFileLockLifetimeWarning`` once you have accepted the overlap.
Migrate to ``SoftFileLease`` when expiry
is required or ``StrictSoftFileLock`` when unknown and stale claims must fail closed. Async callers use
``AsyncSoftFileLease`` or ``AsyncStrictSoftFileLock``. ``lifetime=None`` disables age-based removal; same-host dead-PID
and malformed-record recovery still apply.

``SoftFileLock`` honors the value at construction and on assignment. A ``UnixFileLock`` accepts an assignment only
after it has fallen back on the current resolved path. Other locks either drop the value with a ``UserWarning`` naming
why that backend cannot age out a holder, or refuse the keyword outright:

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
    - - ``UnixFileLock`` after fallback, assigned non-``None``
      - Removes a soft marker after the configured age on that resolved path.
      - A live holder may overlap its successor.
    - - Native lock, non-``None``
      - Warns and ignores: a kernel lock cannot be broken safely by file age.
      - Kernel lock ownership is unchanged.
    - - ``StrictSoftFileLock``, non-``None``
      - Warns and ignores: a strict claim is never broken by age, only by ``force_break()``.
      - Unchanged; a crashed holder's claim still waits for an operator.
    - - ``SoftFileLease``, non-``None``
      - Warns and ignores: ``lease_duration`` already sets when the claim expires.
      - Unchanged; expiry follows ``lease_duration``.
    - - ``ReadWriteLock`` or ``SoftReadWriteLock``, any value
      - Raises :class:`TypeError`; these constructors take no ``lifetime``.
      - Not applicable.

Native locks (:class:`UnixFileLock <filelock.UnixFileLock>` and
:class:`WindowsFileLock <filelock.WindowsFileLock>`) ignore a non-``None`` constructor value and emit a warning. A
``UnixFileLock`` also ignores an assignment before fallback or on another resolved path. The same rules apply when
:class:`FileLock <filelock.FileLock>` selects one of those backends; on a build without ``fcntl``, ``FileLock`` may
instead alias ``SoftFileLock``. A kernel lock lives on the inode, so pathname age cannot revoke it. The async peers
behave as their synchronous counterparts do.

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
        box rgba(21, 101, 192, 0.16) Worker
            participant W as Worker Thread
        end
        box rgba(230, 81, 0, 0.16) Coordination
            participant L as File Lock
        end
        box rgba(69, 39, 160, 0.16) Control
            participant M as Main Thread
        end
        W->>+L: acquire(cancel_check=shutdown.is_set)
        activate W
        loop Every poll_interval
            L->>L: Try lock (busy)
            L->>W: Check cancel_check()
            W-->>L: False (keep waiting)
        end
        activate M
        M->>M: shutdown.set()
        deactivate M
        L->>W: Check cancel_check()
        W-->>L: True (cancel!)
        L->>-W: Raise Timeout
        deactivate W
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

On a thread-local synchronous lock (the default), ``force=True`` also releases holds left by worker threads that exited.
The main thread keeps its hold through shutdown so its exit callbacks can use it. Running workers keep their holds.
Dropping the last reference to the lock releases every thread's hold. A thread-local async lock releases only the calling thread's hold.

During a synchronous ``acquire()``, a hook or signal handler can enter and leave its own nested ``with lock:`` block.
Calling ``release()`` to drop the level that ``acquire()`` is still taking raises :exc:`RuntimeError`. If the exception
escapes the hook or handler, the acquire rolls back and any earlier hold keeps its depth.

A handler that calls ``release(force=True)`` drops every level. If it returns, an acquire that observes the release
raises :exc:`RuntimeError`. A force release just as the acquire returns can leave the caller entering its block with
the lock already released.

Do not release from a signal handler. Python runs the handler on the main thread between two bytecodes of whatever it
was doing, which can be the middle of a ``release()`` of the same lock. Raise from the handler instead, and let the
``with`` block or a ``finally`` release the lock as the stack unwinds:

.. code-block:: python

    import signal

    from filelock import FileLock


    def stop(signum, frame):
        raise SystemExit(1)


    signal.signal(signal.SIGTERM, stop)

    with FileLock("work.lock"):
        ...  # SIGTERM unwinds through here and the with block releases the lock

A ``release()`` that does run inside a handler while the interrupted code was already releasing that lock on the same
thread returns without doing anything, and the interrupted release finishes the job. An ``acquire()`` inside a handler
while the interrupted code is still acquiring that lock raises :class:`RuntimeError`: the interrupted acquire cannot go
on until the handler returns.

On Windows, :class:`~filelock.SoftFileLock` can report ``is_locked == False`` while ``release()`` is still trying to
delete the marker. If a signal handler calls ``acquire()`` on another instance for that path, it can block waiting
for the marker to disappear. Cleanup cannot resume until the handler returns, so the default blocking call can wait forever.
Have the handler set a flag and return.

After the ``with`` block exits, Windows may still be denying deletion of the marker. Only the instance that released
the lock can retry its pending cleanup. If the flag is set and you need the lock again, reuse that instance with a
finite timeout, for example ``with lock.acquire(timeout=5):``. Handle :exc:`~filelock.Timeout` if the marker stays in
place. A fresh instance can wait indefinitely for that marker with the default timeout.

If a handler raises ``KeyboardInterrupt`` during release, the lock finishes releasing before the exception propagates.
A handler that fires as ``release()`` begins can leave the hold for the next call.

If a native Unix unlock raises an exception with an ``errno``, the instance cannot tell whether the kernel released the
lock. It keeps the descriptor for an explicit ``release()`` retry and refuses another ``acquire()`` on the
same instance until that retry succeeds. The first exception still propagates.

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

These properties reflect the state of your lock instance only. After a failed native Unix unlock, ``is_locked`` can
remain ``True`` even if the kernel released the lock. Call ``release()`` again before reusing the instance. To check if
*another* process holds the lock, try to acquire with ``blocking=False``:

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

When both errors subclass :class:`Exception`, the group is a plain :class:`ExceptionGroup`, so ``except*`` and
``except Exception`` still catch it. The default ``"chain"`` keeps Python's behavior.

filelock also groups the failures of its own cleanup, such as an ``on_acquired`` hook that raises and a rollback
release that fails after it. These failures raise a :class:`BaseExceptionGroup`.

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

On Unix, when ``flock`` returns ``ENOSYS``, :class:`UnixFileLock <filelock.UnixFileLock>` follows
:class:`SoftFileLock <filelock.SoftFileLock>` semantics for that resolved path by default. If the application requires
the native backend, construct ``UnixFileLock`` with ``fallback_to_soft=False`` so ``ENOSYS`` propagates:

.. code-block:: python

    from filelock import UnixFileLock

    lock = UnixFileLock("work.lock", fallback_to_soft=False)

``ENOSYS`` then propagates as the original :class:`OSError` from :func:`fcntl.flock`, with
:data:`errno.ENOSYS`; filelock does not wrap it.

After fallback, ``UnixFileLock`` keeps its class. Use ``pid`` and ``is_lock_held_by_us`` to inspect the soft marker,
``lifetime`` to set its expiry, and ``break_lock()`` to clear it. On a native path, the marker properties raise
:class:`AttributeError`, ``break_lock()`` raises :class:`RuntimeError`, and assigning ``lifetime`` emits a warning.

If the requested mode permits writes by other accounts, filelock probes a private file in the same directory before
creating the lock file. On a filesystem without ``flock``, the new marker excludes group and other writes from creation.
Existing lock files need no private probe.

If the directory denies the probe, filelock creates the target without group or other writes and tests ``flock`` there.
A native lock then applies an explicit ``mode``. With the default mode, the new file keeps the restricted permissions.
Set ``mode=0o660`` for a group-writable native lock in such a directory.

If a private probe succeeds but only the target rejects ``flock``, filelock needs atomic exchange to replace a writable
target. Without exchange it raises :class:`PermissionError` and can leave an empty marker.

Where atomic exchange is unavailable, an interrupt during publication may leave an empty marker or the acquiring
process's PID after the acquire failed. Check that no process holds the lock before clearing that marker with
``break_lock()``.

Two other options imply the same refusal, because a soft lock cannot honor either: ``preserve_lock_file=True`` (a soft
lock releases by unlinking its marker) and ``on_acquired`` (a soft lock keeps protocol state in the marker and has no
native descriptor to lend). Setting either makes ``UnixFileLock`` raise on ``ENOSYS`` even with the default
``fallback_to_soft=True``, rather than silently downgrade and drop the guarantee you asked for:

.. code-block:: python

    # Each of these raises OSError(ENOSYS) on a filesystem without flock, instead of downgrading.
    UnixFileLock("work.lock", fallback_to_soft=False)
    UnixFileLock("work.lock", preserve_lock_file=True)
    UnixFileLock("work.lock", on_acquired=write_holder)

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

A common use is writing holder metadata into the lock file. filelock does not empty an existing lock file, so the hook
truncates what an earlier holder wrote:

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

For the same reason, a hook makes :class:`UnixFileLock <filelock.UnixFileLock>` fail closed on a filesystem whose
``flock`` returns ``ENOSYS``: the soft backend it would otherwise fall back to has no descriptor to pass, so downgrading
would drop the callback without telling you. See :ref:`how-to:Fail closed instead of downgrading to soft`.

*******************************************
 Lock a descriptor you already own
*******************************************

:func:`lock_descriptor <filelock.lock_descriptor>` and :func:`unlock_descriptor <filelock.unlock_descriptor>` take and
release the same native lock :class:`FileLock <filelock.FileLock>` uses, but on a file descriptor you opened
and own. They add no path handling: no open, truncate, close, unlink, chmod, canonicalize, or fallback. A descriptor
lock and a native ``FileLock`` path lock on the same file contend with each other.

On Unix this is a whole-file :func:`fcntl.flock` lock. On Windows it is a ``LockFileEx`` lock of one byte at offset 0.
Neither function accepts an offset or length, and seeking the descriptor does not change the locked range.

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
``blocking`` must be a :class:`bool`; passing ``0`` or ``"false"`` raises :class:`TypeError`.
Blocking calls require a positive ``poll_interval`` of at most :data:`threading.TIMEOUT_MAX`. There is no async
wrapper. Run it in an executor, or drive ``blocking=False`` from your own polling loop. On Windows *fd* must be a
synchronous descriptor.

To interoperate with another tool, all participants must agree on the file, locking primitive, range, and protocol.
For example, `conda's repodata lock
<https://github.com/conda/conda/blob/main/conda/gateways/disk/lock.py>`_ locks one byte at offset 21 using
:func:`fcntl.lockf` on Unix or :func:`msvcrt.locking` on Windows. ``lock_descriptor`` is not a portable replacement for
that protocol: the Windows ranges do not overlap, and Unix ``flock`` and ``lockf`` are different primitives whose
interaction depends on the operating system and filesystem. Use the primitive and range required by the existing
protocol; sharing a file descriptor alone does not establish compatibility.

Both functions raise ``OSError`` with :data:`errno.ENOSYS` when the Python build lacks the native locking primitive.
The :class:`FileLock <filelock.FileLock>` and :class:`AsyncFileLock <filelock.AsyncFileLock>` aliases continue to select
their soft implementations on those builds. For timeout, reentrancy, singleton, lifetime, or stale-break behavior, use
:class:`FileLock <filelock.FileLock>`.
