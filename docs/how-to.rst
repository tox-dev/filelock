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

Sometimes you want to attempt the lock exactly once—either you get it immediately or you don't.

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

The ``blocking`` parameter takes precedence over ``timeout``\ —if you set both, ``blocking`` wins:

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

Note that async locks default to ``thread_local=False`` (unlike sync locks which default to ``True``) because the
acquiring and releasing threads may differ when using an executor.

Available async lock classes:

- :class:`AsyncFileLock <filelock.AsyncFileLock>` — platform-aware (recommended).
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
    lock_b.acquire()  # Reentrant—lock counter is now 2
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

**************************************
 Detect stale locks (soft locks only)
**************************************

:class:`SoftFileLock <filelock.SoftFileLock>` stores the PID and hostname of the lock holder. On Unix and macOS, it can
detect when the holding process has died and automatically break stale locks.

This happens automatically—you don't need to do anything special:

.. code-block:: python

    from filelock import SoftFileLock

    lock = SoftFileLock("work.lock")

    with lock:
        # If the process holding the lock dies,
        # another process will automatically clean up the stale lock
        pass

Stale lock detection only works on Unix/macOS and only detects locks from the same host. Cross-host stale locks still
require manual removal.

On Windows, stale lock detection is skipped because the lock file cannot be atomically renamed while another process
holds a handle to it.

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

You can create locks that automatically expire after a certain time:

.. code-block:: python

    from filelock import FileLock

    # Lock expires after 3600 seconds (1 hour)
    lock = FileLock("work.lock", lifetime=3600)

    with lock:
        # Lock is held, but will auto-expire after 1 hour
        pass

This is useful for distributed systems where a process might crash and leave a lock behind. After the lifetime expires,
other processes can acquire it automatically.

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
    print(lock.is_locked)  # False — fully released

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
