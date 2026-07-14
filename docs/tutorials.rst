###########
 Tutorials
###########

This section guides you through the fundamentals of file locking, starting with the basics and building up to advanced
patterns.

*****************
 Your first lock
*****************

Let's create our first lock and use it to coordinate between processes.

We import what we need and create a lock object:

.. code-block:: python

    from pathlib import Path
    from filelock import FileLock

    lock = FileLock("myapp.lock")

Now we have a lock object that represents a lock file on disk. We can use the lock with a context manager (the ``with``
statement):

.. code-block:: python

    with lock:
        # Inside this block, we hold the lock
        print("I have the lock!")
    # Outside this block, the lock is released

Run this code in several terminal windows at the same time. Only one process prints the message at a time; the others
wait for their turn.

************************
 Protecting shared data
************************

File locks are most useful when protecting data that multiple processes access:

.. code-block:: python

    from pathlib import Path
    from filelock import FileLock

    data_file = Path("data.txt")
    lock = FileLock("data.txt.lock")

    # Process A writes a greeting
    with lock:
        if not data_file.exists():
            data_file.write_text("Hello from Process A\n")

    # Process B appends another greeting
    with lock:
        with data_file.open("a") as f:
            f.write("Hello from Process B\n")

Before making changes, check what is already done. Process A checks whether the file exists before
writing. Process B appends, so it skips the check. Both use the lock so that only one process modifies the file at a
time.

Run this code from two different processes. The file will contain messages from both in a consistent order.

*****************
 Reentrant locks
*****************

Sometimes you need to acquire the same lock multiple times from the same process or thread. The lock allows this:

.. code-block:: python

    from filelock import FileLock

    lock = FileLock("reentrant.lock")


    def helper_function():
        with lock:
            print("Helper has the lock")


    with lock:
        print("Main code has the lock")
        helper_function()  # Can acquire the same lock again
        print("Still have the lock")

No deadlock occurs. The lock counts how many times you acquire it and releases only when the count reaches zero. You can
inspect this counter and the lock state at any time:

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

You can call functions that acquire a lock even while you already hold it.

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

Keep SQLite-backed :class:`ReadWriteLock <filelock.ReadWriteLock>` on a local filesystem supported by the active SQLite
VFS. On a shared filesystem, use :class:`SoftReadWriteLock <filelock.SoftReadWriteLock>` only after verifying exclusive
creation, rename, unlink, timestamps, and cache visibility across participating hosts. Heartbeat expiry permits another
holder to enter if an old process pauses and later resumes.

.. code-block:: python

    from filelock import SoftReadWriteLock

    rw = SoftReadWriteLock("/shared/nfs/work.lock")

    with rw.read_lock():
        # Cooperating readers can hold the lock together.
        data = open("/shared/nfs/data.json").read()

    with rw.write_lock():
        # New readers wait behind an observed writer marker.
        open("/shared/nfs/data.json", "w").write(new_data)

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

See :doc:`concepts` for the full explanation of the heartbeat + TTL model.

************
 Next steps
************

- Want to handle timeouts, cancellation, or force-release? See :doc:`how-to`.
- Curious about how locks work across different platforms? Read :doc:`concepts`.
