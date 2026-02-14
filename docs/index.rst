filelock
========

A platform-independent file locking library for Python, providing inter-process synchronization:

.. code-block:: python

    from filelock import Timeout, FileLock

    lock = FileLock("high_ground.txt.lock")
    with lock:
        with open("high_ground.txt", "a") as file_handler:
            file_handler.write("You were the chosen one.")

**Don't use** a :class:`FileLock <filelock.FileLock>` to lock the file you want to write to, instead create a separate
``.lock`` file as shown above.

.. image:: example.gif
  :alt: Example gif


Similar libraries
-----------------

Perhaps you are looking for something like:

- the `pid <https://pypi.org/project/pid/>`_ 3rd party library,
- for Windows the `msvcrt <https://docs.python.org/3/library/msvcrt.html#msvcrt.locking>`_ module in the standard
  library,
- for UNIX the `fcntl <https://docs.python.org/3/library/fcntl.html#fcntl.flock>`_ module in the standard library,
- the `flufl.lock <https://pypi.org/project/flufl.lock/>`_ 3rd party library,
- the `fasteners <https://pypi.org/project/fasteners/>`_ 3rd party library.


Installation
------------

``filelock`` is available via PyPI, so you can pip install it:

.. code-block:: bash

    python -m pip install filelock

Usage
-----

Tutorial
^^^^^^^^

A :class:`FileLock <filelock.FileLock>` is used to indicate to another process of your application that a resource or
working directory is currently in use. To do so, create a :class:`FileLock <filelock.FileLock>` first:

.. code-block:: python

    from pathlib import Path

    from filelock import Timeout, FileLock

    file_path = Path("high_ground.txt")
    lock_path = Path("high_ground.txt.lock")

    lock = FileLock(lock_path, timeout=1)

The lock object represents an exclusive lock and can be acquired in multiple ways, including the ones used to acquire
standard Python thread locks:

.. code-block:: python

    with lock:
        if not file_path.exists():
            file_path.write_text("Hello there!")
    # here, all processes can see consistent content in the file

    lock.acquire()
    try:
        if not file_path.exists():
            file_path.write_text("General Kenobi!")
    finally:
        lock.release()
    # here, all processes can see consistent content in the file

    @lock
    def decorated():
        print("You're a decorated Jedi!")


    decorated()

Note: When a process gets the lock (i.e. within the ``with lock:`` region), it is usually good to check what has
already been done by other processes. For example, each process above first checks the existence of the file. If
it is already created, we should not destroy the work of other processes. This is typically the case when we want
just one process to write content into a file, and let every process read the content.

The lock objects are reentrant, which means that once acquired, they will not block on successive lock requests:

.. code-block:: python

    def cite1():
        with lock:
            with file_path.open("a") as file_handler:
                file_handler.write("I hate it when he does that.")


    def cite2():
        with lock:
            with file_path.open("a") as file_handler:
                file_handler.write("You don't want to sell me death sticks.")


    # The lock is acquired here.
    with lock:
        cite1()
        cite2()
    # And released here.

It should also be noted that the lock is released during garbage collection.
Put another way, if you do not assign an acquired lock to a variable,
the lock will eventually be released (implicitly, not explicitly).
For this reason, using the lock in the way shown below is not something you should ever do,
always use the context manager (``with`` form) instead.

This issue is illustrated with code below:

.. code-block:: python

    # If you create a lock and acquire it but don't assign it,
    # you will not actually hold the lock forever.
    # Instead, the lock is released
    # when the created variable is garbage collected.
    FileLock(lock_path).acquire()
    # At some point after the creation above,
    # the lock is released again even though there is no explicit call to `release`.

    # If you instead assign to a dummy variable,
    # the lock will be held
    _ = FileLock(lock_path).acquire()
    # Now the lock is being held
    # (at least until `_` is reassigned and the lock is garbage collected)

Timeouts and non-blocking locks
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

The :meth:`acquire <filelock.BaseFileLock.acquire>` method accepts a ``timeout`` parameter. If the lock cannot be
acquired within ``timeout`` seconds, a :class:`Timeout <filelock.Timeout>` exception is raised:

.. code-block:: python

    try:
        with lock.acquire(timeout=10):
            with file_path.open("a") as file_handler:
                file_handler.write("I have a bad feeling about this.")
    except Timeout:
        print("Another instance of this application currently holds the lock.")

Using a ``timeout < 0`` makes the lock block until it can be acquired
while ``timeout == 0`` results in only one attempt to acquire the lock before raising a :class:`Timeout <filelock.Timeout>` exception (-> non-blocking).

You can also use the ``blocking`` parameter to attempt a non-blocking :meth:`acquire <filelock.BaseFileLock.acquire>`.

.. code-block:: python

    try:
        with lock.acquire(blocking=False):
            with file_path.open("a") as file_handler:
                file_handler.write("I have a bad feeling about this.")
    except Timeout:
        print("Another instance of this application currently holds the lock.")


The ``blocking`` option takes precedence over ``timeout``.
Meaning, if you set ``blocking=False`` while ``timeout > 0``, a :class:`Timeout <filelock.Timeout>` exception is raised without waiting for the lock to release.

You can pre-parametrize both of these options when constructing the lock for ease-of-use.

.. code-block:: python

    from filelock import Timeout, FileLock

    lock_1 = FileLock("high_ground.txt.lock", blocking = False)
    try:
        with lock_1:
            # do some work
            pass
    except Timeout:
        print("Well, we tried once and couldn't acquire.")

    lock_2 = FileLock("high_ground.txt.lock", timeout = 10)
    try:
        with lock_2:
            # do some other work
            pass
    except Timeout:
        print("Ten seconds feel like forever sometimes.")

Poll interval
^^^^^^^^^^^^^

When the lock cannot be acquired immediately, the :meth:`acquire <filelock.BaseFileLock.acquire>` method retries at a
fixed interval. The ``poll_interval`` parameter controls how many seconds to wait between attempts (default ``0.05``).

You can pass ``poll_interval`` directly to :meth:`acquire <filelock.BaseFileLock.acquire>`:

.. code-block:: python

    with lock.acquire(poll_interval=0.1):
        with file_path.open("a") as file_handler:
            file_handler.write("Patience you must have, my young Padawan.")

Or set it on the constructor so that it applies when using the lock as a context manager:

.. code-block:: python

    lock = FileLock("high_ground.txt.lock", poll_interval=0.25)
    with lock:
        with file_path.open("a") as file_handler:
            file_handler.write("This is where the fun begins.")

The default can also be changed at any time via the :attr:`~filelock.BaseFileLock.poll_interval` property:

.. code-block:: python

    lock.poll_interval = 0.5

Logging
^^^^^^^

All log messages by this library are made using the ``DEBUG`` level, under the ``filelock`` name. On how to control
displaying/hiding that please consult the
`logging documentation of the standard library <https://docs.python.org/3/howto/logging.html>`_. E.g. to hide these
messages you can use:

.. code-block:: python

    logging.getLogger("filelock").setLevel(logging.INFO)

Lock types
----------

Choosing the right lock
^^^^^^^^^^^^^^^^^^^^^^^

This library provides several lock implementations. Pick the one that matches your use case:

- :class:`FileLock <filelock.FileLock>` (recommended default) -- a platform-aware alias that resolves to the best
  available backend at import time:

  - **Windows** -- :class:`WindowsFileLock <filelock.WindowsFileLock>` (``msvcrt.locking``)
  - **Unix / macOS** -- :class:`UnixFileLock <filelock.UnixFileLock>` (``fcntl.flock``)
  - **Other** (no ``fcntl``) -- :class:`SoftFileLock <filelock.SoftFileLock>` (file-existence fallback, emits a warning)

  Always import :class:`FileLock <filelock.FileLock>` rather than a platform-specific class unless you have a reason
  to pin the backend. For async code, use :class:`AsyncFileLock <filelock.AsyncFileLock>` (same platform resolution).

  *Limitations*: exclusive only -- no shared/reader mode. May not work correctly on some network filesystems (e.g. NFS)
  where OS-level locking is unreliable.

- :class:`SoftFileLock <filelock.SoftFileLock>` -- portable file-existence lock. Works on any filesystem, including
  network mounts where OS-level locking may be unavailable. Async variant:
  :class:`AsyncSoftFileLock <filelock.AsyncSoftFileLock>`.

  The lock file stores the holder's PID and hostname. On **Unix and macOS**, when a competing process finds an existing
  lock, it checks whether the holder is still alive (same host only). If the holding process has died, the stale lock
  is automatically broken via an atomic rename-and-delete sequence. Stale locks from a different host, or lock files
  with unrecognized content (e.g. from an older version), are left untouched and fall back to the normal retry/timeout
  behavior.

  *Limitations*: stale lock detection is **not available on Windows** -- Python's C runtime (``_wopen``) cannot set
  ``FILE_SHARE_DELETE``, so any read handle on the lock file blocks ``DeleteFileW`` in the release path, causing a
  livelock under threaded contention. On Unix/macOS, stale detection only works when the holder and competitor are on
  the same host -- cross-host stale locks still require manual removal. Exclusive only. See the TOCTOU warning below.

- :class:`ReadWriteLock <filelock.ReadWriteLock>` -- shared reads / exclusive writes via SQLite. Use this when you need
  concurrent readers with occasional writers. See :ref:`read-write-lock` below for details.

  *Limitations*: higher overhead than ``FileLock`` due to SQLite transactions. Creates a ``.db`` file on disk.
  No async variant. Cannot upgrade a read lock to a write lock (or vice versa). May not work on network filesystems
  where SQLite locking is unreliable. Requires the ``sqlite3`` standard library module (unavailable if Python was built
  without SQLite support).

.. warning::

   **Security Consideration - TOCTOU Vulnerability**: On platforms without ``O_NOFOLLOW`` support
   (such as GraalPy), :class:`SoftFileLock <filelock.SoftFileLock>` may be vulnerable to symlink-based
   Time-of-Check-Time-of-Use (TOCTOU) attacks. An attacker with local filesystem access could create
   a symlink at the lock file path during the small race window between permission validation and file
   creation.

   On most modern platforms with ``O_NOFOLLOW`` support, this vulnerability is mitigated by refusing
   to follow symlinks when creating the lock file.

   For security-sensitive applications, prefer :class:`UnixFileLock <filelock.UnixFileLock>` or
   :class:`WindowsFileLock <filelock.WindowsFileLock>` which provide stronger guarantees via OS-level
   file locking. :class:`SoftFileLock <filelock.SoftFileLock>` should only be used as a fallback mechanism
   on platforms where OS-level locking primitives are unavailable.

.. _read-write-lock:

ReadWriteLock
^^^^^^^^^^^^^

:class:`ReadWriteLock <filelock.ReadWriteLock>` provides cross-process shared/exclusive locking backed by SQLite
transactions. Multiple processes can hold a read lock simultaneously, but a write lock is exclusive. Use this instead
of :class:`FileLock <filelock.FileLock>` when your workload is read-heavy and you want readers to proceed without
blocking each other. If you only need exclusive locking, prefer :class:`FileLock <filelock.FileLock>` -- it is
lighter and faster.

.. note::

   :class:`ReadWriteLock <filelock.ReadWriteLock>` requires the ``sqlite3`` standard library module. If Python was built
   without SQLite support, importing ``ReadWriteLock`` from ``filelock`` will return ``None``.

Use the ``read_lock()`` and ``write_lock()`` context managers for the most common pattern:

.. code-block:: python

    from pathlib import Path

    from filelock import ReadWriteLock

    rw = ReadWriteLock("data.db")
    data_path = Path("data.txt")

    # Multiple processes/threads can read concurrently
    with rw.read_lock():
        data = data_path.read_text()

    # Only one process/thread can write at a time
    with rw.write_lock():
        data_path.write_text("new content")

For more control, use the low-level ``acquire_read()`` / ``acquire_write()`` / ``release()`` methods:

.. code-block:: python

    rw.acquire_write(timeout=5)
    try:
        data_path.write_text("new content")
    finally:
        rw.release()

The lock is reentrant within the same mode -- nested read locks or nested write locks (from the same thread) work:

.. code-block:: python

    with rw.read_lock():
        with rw.read_lock():  # OK, reentrant
            pass

Upgrading from read to write (or downgrading from write to read) raises ``RuntimeError``:

.. code-block:: python

    with rw.read_lock():
        with rw.write_lock():  # RuntimeError: upgrade not allowed
            pass

Write locks are pinned to the thread that acquired them. A different thread attempting to re-enter an existing write
lock raises ``RuntimeError``.

.. note::

   The lock file is a SQLite database. Use a ``.db`` extension by convention.

FileLocks and threads
^^^^^^^^^^^^^^^^^^^^^

By default the :class:`FileLock <filelock.FileLock>` internally uses :class:`threading.local <threading.local>`
to ensure that the lock is thread-local. If you have a use case where you'd like an instance of ``FileLock`` to be shared
across threads, you can set the ``thread_local`` parameter to ``False`` when creating a lock. For example:

.. code-block:: python

    lock = FileLock("test.lock", thread_local=False)
    # lock will be re-entrant across threads

    # The same behavior would also work with other BaseFileLock subclasses like SoftFileLock:
    from filelock import SoftFileLock

    soft_lock = SoftFileLock("soft_test.lock", thread_local=False)
    # soft_lock will be re-entrant across threads.


Behavior where :class:`FileLock <filelock.FileLock>` is thread-local started in version 3.11.0. Previous versions
were not thread-local by default.

Note: If disabling thread-local, be sure to remember that locks are re-entrant: You will be able to
:meth:`acquire <filelock.BaseFileLock.acquire>` the same lock multiple times across multiple threads.

Singleton locks
^^^^^^^^^^^^^^^

All lock classes accept an ``is_singleton`` parameter. When ``True``, constructing a lock with the same file path
returns the existing instance instead of creating a new one:

.. code-block:: python

    from filelock import FileLock

    a = FileLock("test.lock", is_singleton=True)
    b = FileLock("test.lock", is_singleton=True)
    assert a is b  # same instance

This is useful for reentrant locking without passing the same object around. Acquiring through one reference counts
toward the same lock level:

.. code-block:: python

    a.acquire()
    b.acquire()   # reentrant, lock_counter == 2
    b.release()
    a.release()   # fully released

Parameters are fixed at creation time. Requesting a singleton with different ``timeout``, ``mode``, or ``blocking``
raises ``ValueError``.

.. note::

   :class:`FileLock <filelock.FileLock>` and other :class:`BaseFileLock <filelock.BaseFileLock>` subclasses default to
   ``is_singleton=False``. :class:`ReadWriteLock <filelock.ReadWriteLock>` defaults to ``is_singleton=True``.

Asyncio support
^^^^^^^^^^^^^^^

Each :class:`BaseFileLock <filelock.BaseFileLock>` subclass has an async counterpart that can be used with
``async with`` (:class:`ReadWriteLock <filelock.ReadWriteLock>` does not have an async variant):

- :class:`AsyncFileLock <filelock.AsyncFileLock>`
- :class:`AsyncSoftFileLock <filelock.AsyncSoftFileLock>`
- :class:`AsyncUnixFileLock <filelock.AsyncUnixFileLock>`
- :class:`AsyncWindowsFileLock <filelock.AsyncWindowsFileLock>`

.. code-block:: python

    from pathlib import Path

    from filelock import AsyncFileLock

    lock = AsyncFileLock("high_ground.txt.lock")

    async with lock:
        with Path("high_ground.txt").open("a") as file_handler:
            file_handler.write("You were the chosen one.")

By default, the underlying blocking I/O runs in a thread-pool executor (``run_in_executor=True``). You can pass a
custom executor or set ``run_in_executor=False`` to run the I/O directly in the event loop (only advisable when the
filesystem is known to be non-blocking).

.. warning::

   ``thread_local=True`` and ``run_in_executor=True`` are incompatible. Creating a lock with both raises
   ``ValueError``. Async locks default to ``thread_local=False``.

Contributions and issues
------------------------

Contributions are always welcome, please make sure they pass all tests before creating a pull request. This project is
hosted on `GitHub <https://github.com/tox-dev/py-filelock>`_. If you have any questions or suggestions, don't hesitate
to open a new issue 😊. There's no bad question, just a missed opportunity to learn more.

.. toctree::
   :hidden:

   self
   api
   license
   changelog
