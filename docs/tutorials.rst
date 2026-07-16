###########
 Tutorials
###########

This section guides you through the fundamentals of file locking, starting with the basics and building up to advanced
patterns. Each lesson is built around a job real programs hand to a file lock: guarding a download cache, memoizing an
expensive probe, coordinating readers and writers on a shared filesystem.

*****************
 Your first lock
*****************

Start with the job that pulls most projects to a file lock in the first place: two copies of a program racing to
populate the same cache entry.

`huggingface_hub <https://github.com/huggingface/huggingface_hub>`_ downloads model weights into a shared cache
directory. When two processes want the same file, one should fetch it while the other waits, rather than both streaming
gigabytes over the same connection. It puts a lock beside the cache entry, keyed on the file's ETag:

.. code-block:: python

    from filelock import FileLock

    lock = FileLock("cache/.locks/models/bert-base/a1b2c3.lock")

The lock object represents a lock file on disk. Nothing is locked yet. Use it with a context manager (the ``with``
statement):

.. code-block:: python

    with lock:
        # Inside this block, we hold the lock
        print("I have the lock!")
    # Outside this block, the lock is released

Run this in several terminals at once. Only one process prints at a time; the others wait their turn.

Two details of that path are deliberate, and both come from real deployments:

- The lock lives beside the cache entry, not on it. Locking the target file itself would mean creating the very file
  whose absence you are testing for.
- The lock file name is derived from a hash. Cache keys can be long, and a filesystem caps a single name component
  (255 bytes on ext4 and on Windows). `datasets <https://github.com/huggingface/datasets>`_ hashes lock names against
  ``os.statvfs(path).f_namemax`` for exactly this reason. Hash long keys instead of pasting them into the name.

************************
 Protecting shared data
************************

A lock is worth little on its own. It earns its keep when it wraps a *check, then act* sequence that would otherwise
race.

`virtualenv <https://github.com/pypa/virtualenv>`_ caches what it learns about each Python interpreter it probes, since
launching an interpreter to interrogate it is slow. Two virtualenv runs starting together would both probe, and both
write. The lock turns that into one probe:

.. code-block:: python

    import json
    from pathlib import Path

    from filelock import FileLock

    cache_file = Path("py_info/5/a1b2c3.json")
    lock = FileLock("py_info/5/a1b2c3.json.lock")

    with lock:
        if cache_file.exists():
            info = json.loads(cache_file.read_text())
        else:
            info = probe_interpreter()  # slow: launches the interpreter
            cache_file.write_text(json.dumps(info))

The check and the write live inside the same lock. That is the whole point: if you test ``cache_file.exists()`` outside
the lock and write inside it, two processes can both see it missing and both probe.

.. mermaid::

    sequenceDiagram
        box rgba(21, 101, 192, 0.16) virtualenv runs
            participant A as virtualenv A
            participant B as virtualenv B
        end
        box rgba(230, 81, 0, 0.16) Coordination
            participant L as Lock
        end
        box rgba(46, 125, 50, 0.16) App data
            participant C as Cache file
        end
        activate A
        A->>L: acquire()
        activate L
        B->>L: acquire() (waits...)
        A->>+C: exists()?
        C-->>-A: no
        Note over A: probe interpreter (slow)
        A->>C: write info
        A->>L: release()
        deactivate L
        deactivate A
        L->>B: acquired
        activate B
        activate L
        B->>+C: exists()?
        C-->>-B: yes
        Note over B: reuse, no probe
        B->>L: release()
        deactivate L
        deactivate B

*****************
 Reentrant locks
*****************

Sometimes you need to acquire the same lock again from code already holding it. The lock allows this:

.. code-block:: python

    from filelock import FileLock

    lock = FileLock("py_info/5/a1b2c3.json.lock")


    def read_cached_info():
        with lock:
            return load_info()


    with lock:
        refresh_cache()
        info = read_cached_info()  # acquires the same lock again

No deadlock occurs. The lock counts acquisitions and releases only when the count reaches zero. This is what lets a
helper take the lock defensively without knowing whether its caller already holds it. You can inspect the counter and
state at any time:

.. code-block:: python

    lock = FileLock("reentrant.lock")

    print(lock.is_locked)     # False
    print(lock.lock_counter)  # 0

    lock.acquire()
    print(lock.is_locked)     # True
    print(lock.lock_counter)  # 1

    lock.acquire()
    print(lock.lock_counter)  # 2

    lock.release()
    print(lock.lock_counter)  # 1, still locked
    print(lock.is_locked)     # True

    lock.release()
    print(lock.lock_counter)  # 0, fully released
    print(lock.is_locked)     # False

virtualenv leans on this directly: its ``util/lock.py`` wraps ``FileLock`` in a counted subclass so nested acquisitions
of the same app-data entry are cheap.

*****************************
 Multiple ways to use a lock
*****************************

So far we've used the ``with`` statement. There are other ways:

**Manual acquire and release:**

.. code-block:: python

    lock.acquire()
    try:
        print("I have the lock")
    finally:
        lock.release()

Always use a ``try/finally`` block to guarantee the lock is released, even if an exception occurs.

**As a decorator:**

.. code-block:: python

    @lock
    def protected_operation():
        print("This function runs with the lock held")


    protected_operation()  # Lock is acquired, function runs, lock is released

Choose whichever feels most natural for your code. The ``with`` statement reads clearest.

Important: Always use the context manager
=========================================

Avoid this pattern:

.. code-block:: python

    FileLock("my.lock").acquire()  # ⚠️ Don't do this
    # The lock might be released during garbage collection
    # before your code finishes

This doesn't work reliably because if you don't assign the lock to a variable, Python's garbage collector might release
it before you're done with it.

Instead, always keep a reference to the lock object:

.. code-block:: python

    lock = FileLock("my.lock")
    with lock:  # ✓ Good
        # your code here
        pass

**************************
 Thread-local by default
**************************

By default, each lock uses thread-local state (``thread_local=True``): each thread tracks its own lock counter. Two
threads holding the same ``FileLock`` object each get their own reentrant state.

Async locks default to ``thread_local=False`` because they run in a thread pool where the acquiring thread may differ
from the releasing thread.

See :ref:`how-to:Use locks with multiple threads` for practical examples of controlling this behavior.

**********************
 Your first async lock
**********************

An async program cannot afford a blocking ``acquire()``: while one coroutine waits on the filesystem, the event loop
should keep serving everything else. Use the async variants with ``async with``:

.. code-block:: python

    from pathlib import Path

    from filelock import AsyncFileLock

    lock = AsyncFileLock("cache/.locks/models/bert-base/a1b2c3.lock")


    async def fetch_once(url: str) -> bytes:
        async with lock:
            blob = Path("cache/models/bert-base/a1b2c3.bin")
            if not blob.exists():
                blob.write_bytes(await download(url))
            return blob.read_bytes()

The lock runs its blocking filesystem calls in a thread pool, so the event loop stays free while you wait.

.. warning::

   Plain ``with`` does not work on an async lock. ``acquire`` and ``release`` are coroutines; use ``async with``.

Every lock type in this tutorial has an async peer: :class:`AsyncSoftFileLock <filelock.AsyncSoftFileLock>`,
:class:`AsyncStrictSoftFileLock <filelock.AsyncStrictSoftFileLock>`,
:class:`AsyncSoftFileLease <filelock.AsyncSoftFileLease>`, :class:`AsyncReadWriteLock <filelock.AsyncReadWriteLock>`,
and :class:`AsyncSoftReadWriteLock <filelock.AsyncSoftReadWriteLock>`. They share the on-disk protocol of their
synchronous counterparts, so a sync and an async process contend correctly with each other. See
:ref:`how-to:Use async locks`.

*******************************
 Reading who holds a soft lock
*******************************

A native lock lives in the kernel, so there is nothing on disk to read: the file is a handle, not a record. A soft lock
is the opposite. Its marker file *is* the protocol, and it names its owner.

This is what lets an operator answer "what is holding this thing?" without attaching a debugger. PostgreSQL is the
canonical example: it writes ``postmaster.pid`` into the data directory so a second server, and a human, can see who
claims it.

:class:`SoftFileLock <filelock.SoftFileLock>` publishes a PID and hostname:

.. code-block:: python

    from filelock import SoftFileLock

    lock = SoftFileLock("server.lock")

    with lock:
        print(lock.pid)                 # e.g. 12345
        print(lock.is_lock_held_by_us)  # True

:class:`SoftFileLease <filelock.SoftFileLease>` publishes more. It derives from
:class:`MarkerSoftFileLock <filelock.MarkerSoftFileLock>`, which writes a richer *protocol 2* record and hands it back
as an :class:`OwnerRecord <filelock.OwnerRecord>`:

.. code-block:: python

    from filelock import SoftFileLease

    lease = SoftFileLease("server.lock", lease_duration=30)

    with lease:
        if (owner := lease.owner) is not None:  # None if the marker is missing or unreadable
            print(owner.pid, owner.hostname)
            print(owner.mode)            # "lease"
            print(owner.token)           # names this particular claim
            print(owner.lease_duration)  # 30.0
            print(owner.start)           # process start token, or None on platforms without one

:class:`StrictSoftFileLock <filelock.StrictSoftFileLock>` keeps its owner metadata differently, one record per claim
read through ``lock.claims``, so it has no ``owner`` property.

That ``start`` field is what keeps a recycled PID from reading as a live owner. The operating system reuses PIDs, so
"PID 12345 exists" does not mean "the process that wrote this marker exists". filelock folds in a per-platform start
token (Linux ``/proc/<pid>/stat`` plus the boot id, macOS ``sysctl``, Windows ``GetProcessTimes``) so a contender can
tell the difference.

.. note::

   PostgreSQL writes a start time into ``postmaster.pid`` but does not compare it; it defends against PID reuse
   structurally instead. It is a precedent for the problem, not for this solution.

See :ref:`how-to:Inspect a protocol 2 owner record` for using these records in recovery tooling.

***************************************
 Migrating from lockfile.PIDLockFile
***************************************

If you're migrating from the deprecated `lockfile <https://pypi.org/project/lockfile/>`_ library,
:class:`SoftFileLock <filelock.SoftFileLock>` is the direct replacement for ``PIDLockFile``. It writes the process ID to
the lock file and can detect stale locks.

.. code-block:: python

    # Before (lockfile):
    from lockfile.pidlockfile import PIDLockFile

    lock = PIDLockFile("/tmp/myapp.lock")
    lock.acquire()
    print(lock.read_pid())
    print(lock.is_lock_held_by_us())
    lock.release()

    # After (filelock):
    from filelock import SoftFileLock

    lock = SoftFileLock("/tmp/myapp.lock")

    with lock:
        print(lock.pid)
        print(lock.is_lock_held_by_us)

Key differences from ``PIDLockFile``:

- ``read_pid()`` is now a property: ``lock.pid``
- ``is_lock_held_by_us()`` is now a property: ``lock.is_lock_held_by_us``
- ``break_lock()`` is now ``lock.break_lock()`` (same name)
- Stale lock detection happens automatically on acquire (all platforms)
- Supports context managers, reentrant locking, timeouts, and all other filelock features

*******************************************
 Reader/writer locks on shared filesystems
*******************************************

When many processes read a resource and few write it, an exclusive lock serializes work that never needed serializing.
A reader/writer lock lets readers share and writers exclude.

`restic <https://github.com/restic/restic>`_ is the reference case: many backup clients read a repository at once, while
a prune must run alone. It matters that restic builds this *above* the filesystem, because no atomic reader/writer
primitive survives the object stores and network mounts it targets. filelock draws the same line:

Keep SQLite-backed :class:`ReadWriteLock <filelock.ReadWriteLock>` on a local filesystem supported by the active SQLite
VFS. On a shared filesystem, use :class:`SoftReadWriteLock <filelock.SoftReadWriteLock>` only after verifying exclusive
creation, rename, unlink, timestamps, and cache visibility across participating hosts. Heartbeat expiry permits another
holder to enter if an old process pauses and later resumes.

.. code-block:: python

    from pathlib import Path

    from filelock import SoftReadWriteLock

    rw = SoftReadWriteLock("/shared/nfs/work.lock")
    data_file = Path("/shared/nfs/data.json")

    with rw.read_lock():
        # Cooperating readers can hold the lock together.
        data = data_file.read_text()

    with rw.write_lock():
        # New readers wait behind an observed writer marker.
        data_file.write_text(new_data)

While the lock is held, you will see a few sidecar files on disk next to ``work.lock``:

.. code-block:: text

    work.lock.state         # short-lived state mutex, exists only during transitions
    work.lock.write         # writer marker, exists while a writer is claiming or holding
    work.lock.readers/      # directory with one file per active reader

A daemon heartbeat thread refreshes each marker's ``mtime`` every ``heartbeat_interval`` seconds. A peer may evict a
marker after ``stale_threshold`` seconds without a refresh. Set the threshold above expected process and filesystem
pauses, synchronize participating clocks, and fence protected writes if an expired process can resume:

.. code-block:: python

    rw = SoftReadWriteLock(
        "/shared/nfs/work.lock",
        heartbeat_interval=120,
        stale_threshold=360,
    )

Those two numbers are a ratio, not a pair of independent knobs. restic's repository lock runs the same model with a
5-minute refresh against a 30-minute stale timeout, and treats a lock it could not refresh within 22.5 minutes as lost:
the margin absorbs clock drift and a slow filesystem. See :doc:`concepts` for the full explanation of the heartbeat +
TTL model.

*******************************
 Stronger soft-lock contracts
*******************************

A plain :class:`SoftFileLock <filelock.SoftFileLock>` is a cooperative marker: it reclaims a marker only once its owner
is provably gone, but a shared marker cannot promise that two processes never overlap. When you need more, two soft locks
state an explicit contract.

:class:`StrictSoftFileLock <filelock.StrictSoftFileLock>` gives mutual exclusion without a native lock. It never breaks a
claim by age, so a crashed holder's claim is cleared only by an operator:

.. code-block:: python

    from filelock import StrictSoftFileLock

    lock = StrictSoftFileLock("work.lock")
    with lock:
        do_exclusive_work()

    # After a crash, an operator inspects and clears the stale claim by name:
    stale = StrictSoftFileLock("work.lock")
    for claim in stale.claims:
        stale.force_break(claim.name)

:class:`SoftFileLease <filelock.SoftFileLease>` trades exclusion for progress. Its claim expires, so a peer can take over
if a holder wedges; a callback fires when this process loses its claim. A lease token names a claim but does not fence
the protected resource, so fence any resource an expired holder could still write:

.. code-block:: python

    from filelock import SoftFileLease

    lease = SoftFileLease("work.lock", lease_duration=30, on_compromise=lambda c: stop_work())
    with lease:
        do_resumable_work()

restic and Terraform resolve this choice in opposite directions. Terraform holds its state lock until an operator runs
``force-unlock``, preferring to hang over risking two concurrent applies, while a lease keeps the work moving and leaves
you to fence the resource. Choose according to which failure your system can absorb.

See :ref:`how-to:Choose a soft-lock contract` to pick between them.

************
 Next steps
************

- Waiting on a lock a user can see? See :ref:`how-to:Report progress while waiting` and
  :ref:`how-to:Probe a lock before blocking on it`.
- Need strict exclusion or an expiring lease? See :ref:`how-to:Use fail-closed soft locks` and
  :ref:`how-to:Choose a soft-lock contract`.
- Want to handle timeouts, cancellation, or force-release? See :doc:`how-to`.
- Curious about how locks work across different platforms? Read :doc:`concepts`.
