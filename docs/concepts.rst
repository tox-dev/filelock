#####################
 Concepts and design
#####################

This section explains the ideas behind file locking, how filelock works across different platforms, and the trade-offs
involved in choosing a lock type.

*****************
 Why file locks?
*****************

Multi-process applications often need to coordinate access to shared resources. Without coordination, processes can
interfere with each other and cause data corruption or inconsistent state.

For example, imagine two processes writing to the same configuration file:

.. mermaid::

    sequenceDiagram
        participant A as Process A
        participant File as Configuration File
        participant B as Process B
        A->>File: Read (value: 1)
        B->>File: Read (value: 1)
        Note over A: Increment to 2
        Note over B: Increment to 2
        A->>File: Write 2
        B->>File: Write 2
        Note over File: Final value: 2 (should be 3!)

This scenario is a **race condition**. Process B's write overwrites Process A's changes, and the final result is
incorrect.

File locks prevent this by ensuring only one process can access a resource at a time:

.. mermaid::

    sequenceDiagram
        participant A as Process A
        participant Lock as File Lock
        participant File as Configuration File
        participant B as Process B
        A->>Lock: Acquire lock
        activate Lock
        B->>Lock: Try to acquire (waits...)
        A->>File: Read (value: 1)
        Note over A: Increment to 2
        A->>File: Write 2
        A->>Lock: Release lock
        deactivate Lock
        Lock->>B: Lock acquired!
        B->>File: Read (value: 2)
        Note over B: Increment to 3
        B->>File: Write 3
        B->>Lock: Release lock
        Note over File: Final value: 3 ✓

Now both processes complete successfully and their changes are preserved.

Self-deadlock detection
=======================

A common mistake is creating two separate ``FileLock`` instances for the same file and trying to acquire both in the
same thread:

.. code-block:: python

    lock_a = FileLock("work.lock")
    lock_b = FileLock("work.lock")

    with lock_a:
        with lock_b:  # RuntimeError: Deadlock detected!
            pass

This would normally deadlock forever because ``lock_b`` waits for a lock that ``lock_a`` holds in the same thread.
filelock detects this and raises ``RuntimeError`` with a message suggesting ``is_singleton=True`` as the fix.

This detection only applies to blocking acquires (``timeout < 0``) within the same thread. Non-blocking or timed
acquires raise :class:`Timeout <filelock.Timeout>` as usual.

************************
 How file locking works
************************

There are two fundamentally different approaches to file locking on modern systems:

**OS-level locking** (FileLock on Windows/Unix, UnixFileLock, WindowsFileLock)

The operating system manages locks. When you create a lock, the OS tracks it and enforces it. Only code with an open
file handle can lock it. When a process dies, the OS automatically releases its locks.

.. list-table::
    :header-rows: 1

    - - Pros
      - Cons
    - - ✓ Enforced by the kernel—foolproof.
      - ✗ Exclusive only—no reader/writer distinction (use ReadWriteLock for that).
    - - ✓ Works even if your process crashes.
      - ✗ Unreliable on some network filesystems.
    - - ✓ No network filesystem issues.
      -
    - - ✓ Lower overhead.
      -

**Soft locks** (SoftFileLock, the fallback on systems without fcntl)

A separate "lock file" indicates that a resource is in use. The lock file contains the PID and hostname of the holder. A
process acquires a lock by creating this file; it releases by deleting it.

On Unix/macOS, processes can check if the lock holder is still alive and break stale locks automatically. On Windows,
stale lock breaking is skipped because the lock file cannot be atomically renamed while another process holds a handle.

.. list-table::
    :header-rows: 1

    - - Pros
      - Cons
    - - ✓ Works on any filesystem, including network mounts.
      - ✗ Not enforced—requires cooperation (a buggy process can ignore it).
    - - ✓ Portable—same code works everywhere.
      - ✗ Stale lock detection not available on Windows.
    - - ✓ Can detect stale locks (Unix/macOS only).
      - ✗ Higher overhead than OS-level locks.
    - -
      - ✗ Cross-host detection doesn't work (stale locks from other hosts require manual cleanup).
    - -
      - ✗ On Windows, stale lock detection is skipped (file rename is not atomic while handle is open).

***************************
 Platform-specific details
***************************

**Windows**
    Uses the :class:`WindowsFileLock <filelock.WindowsFileLock>` class, backed by ``msvcrt.locking``. This is enforced
    by Windows, so all code running on the system respects it—whether it uses filelock or not.

    The lock is exclusive and works reliably on local filesystems. Network filesystem (SMB) support is available but
    considered less reliable.

**Unix and macOS**
    Uses the :class:`UnixFileLock <filelock.UnixFileLock>` class, backed by ``fcntl.flock``. This is the POSIX standard
    for file locking and enforced by the kernel.

    Works best on local filesystems. Network filesystems (NFS) may have issues—locking isn't always reliable on NFS even
    in POSIX-compliant systems.

**Other platforms without fcntl**
    Falls back to :class:`SoftFileLock <filelock.SoftFileLock>` and emits a warning. The lock is not enforced by the OS,
    but filelock includes stale lock detection on Unix-like systems (though without fcntl, this detection is less
    reliable than on systems with full fcntl support).

*******************************
 Which lock type should I use?
*******************************

**Start with FileLock** — the platform-aware alias that automatically chooses the best backend:

.. code-block:: python

    from filelock import FileLock

    lock = FileLock("work.lock")

This gives you OS-level locking on Windows and Unix/macOS, with an automatic fallback to soft locks on other systems.
It's the right choice 99% of the time.

**Use UnixFileLock or WindowsFileLock** only if you need to force a specific backend. This is rare and usually only
necessary in testing or if you need platform-specific behavior.

**Use SoftFileLock** when:

- You're on a network filesystem where OS-level locking is unavailable (NFS).
- You need cross-filesystem compatibility and can tolerate the overhead.
- You need stale lock detection (Unix/macOS only).

**Use ReadWriteLock** when:

- Your workload is read-heavy with occasional writes.
- Multiple processes need to read simultaneously.
- You want a single writer while no readers are active.

Lock selection flowchart:

.. mermaid::

    flowchart TD
        start["Choose a lock type"] --> question1{"Read-heavy workload?"}
        question1 -->|Yes| rw["Use ReadWriteLock"]
        question1 -->|No| question2{"Need network<br/>filesystem support?"}
        question2 -->|Yes| soft["Use SoftFileLock"]
        question2 -->|No| question3{"Need platform<br/>specific control?"}
        question3 -->|Yes| platform["Use UnixFileLock<br/>or WindowsFileLock"]
        question3 -->|No| default["Use FileLock<br/>(recommended)"]

        classDef recommended fill:#dcfce7,stroke:#22c55e,stroke-width:2px,color:#14532d
        classDef alternative fill:#fef3c7,stroke:#f59e0b,stroke-width:2px,color:#78350f
        classDef special fill:#dbeafe,stroke:#3b82f6,stroke-width:2px,color:#1e3a5f
        class default recommended
        class soft,platform alternative
        class rw special

Lock types compared
===================

.. list-table::
    :header-rows: 1

    - - Feature
      - FileLock
      - SoftFileLock
      - ReadWriteLock
    - - Exclusive/shared
      - Exclusive only
      - Exclusive only
      - Both (separate context managers)
    - - Platform enforcement
      - OS-level (Windows/Unix)
      - No (file-based)
      - File-based (SQLite)
    - - Network filesystem
      - Not reliable
      - Works (if you accept the limitations)
      - Not reliable
    - - Stale lock detection
      - N/A (OS-enforced)
      - Yes (Unix/macOS only)
      - N/A
    - - Lifetime expiration
      - Yes
      - Yes
      - No
    - - Cancel acquisition
      - Yes (``cancel_check``)
      - Yes (``cancel_check``)
      - No
    - - Force release
      - Yes (``force=True``)
      - Yes (``force=True``)
      - Yes (``force=True``)
    - - Async support
      - AsyncFileLock
      - AsyncSoftFileLock
      - No
    - - Singleton default
      - No
      - No
      - Yes
    - - Overhead
      - Low
      - High
      - Medium (SQLite)

**********************
 TOCTOU vulnerability
**********************

**TOCTOU** (Time-of-Check-Time-of-Use) is a potential security issue: a time gap exists between when you check something
and when you act on it.

For example, this code has a TOCTOU vulnerability:

.. code-block:: python

    if os.path.exists("sensitive.txt"):  # Check
        data = open("sensitive.txt").read()  # Use (race window here!)

An attacker with filesystem access could create a symlink between the check and the use, redirecting you to read a
different file.

How filelock mitigates this:

:class:`SoftFileLock <filelock.SoftFileLock>` on systems without ``O_NOFOLLOW`` support may be vulnerable. But on most
modern platforms (Linux, macOS, Windows), ``O_NOFOLLOW`` is supported, and filelock uses it to refuse following
symlinks.

On older platforms without ``O_NOFOLLOW``, prefer :class:`UnixFileLock <filelock.UnixFileLock>` or
:class:`WindowsFileLock <filelock.WindowsFileLock>` for security-sensitive applications.

***************************************
 What filelock doesn't protect against
***************************************

**Lock files on the actual resource**
    Don't use a lock on the file you want to protect:

    .. code-block:: python

        lock = FileLock("data.txt")  # ⚠️ Wrong!
        with lock:
            data = open("data.txt").read()

    This doesn't work reliably. If you delete ``data.txt``, the lock is gone too. Instead, create a separate lock file:

    .. code-block:: python

        lock = FileLock("data.txt.lock")  # ✓ Right
        with lock:
            data = open("data.txt").read()

**Locks on network filesystems**
    OS-level locks (FileLock on Windows/Unix) are unreliable on network filesystems (NFS, SMB). This is a fundamental
    limitation of how network filesystems work—they don't reliably support locking semantics.

    If you need locking on network filesystems, consider: - Using SoftFileLock (less efficient but more portable) -
    Switching to a centralized lock service (Redis, Consul, etc.) - Using a database with transactions

**Locks across different machines**
    A lock on one machine doesn't stop another machine from accessing the resource unless they use a centralized locking
    service. Filelock is for inter-process coordination on the same machine (or at least the same shared filesystem).

**Read-write semantics**
    FileLock is exclusive only—readers block writers and vice versa. If you need multiple readers with occasional
    writers, use ReadWriteLock instead.

**Atomicity without locks**
    If another process doesn't use the lock, it will still interfere:

    .. code-block:: python

        # Process A (uses lock)
        with lock:
            # Process B might still write at the same time
            # if Process B doesn't use the lock
            open("data.txt").write("A")

    Locks require cooperation—all code accessing a resource must use the same lock.

*******************
 Design trade-offs
*******************

Why not use OS-level locks everywhere?
======================================

OS-level locks (FileLock on Windows/Unix) are fast and enforced by the kernel. But they don't work on all network
filesystems. For portability, filelock includes SoftFileLock as a fallback.

Why does SoftFileLock use a separate file instead of just checking a directory?
===============================================================================

A directory can't reliably be atomically created and deleted across platforms. A file can be created with ``O_EXCL``
(atomic, all-or-nothing) to detect conflicts. This is why SoftFileLock uses files.

Why is stale lock detection only on Unix/macOS?
===============================================

Stale lock detection requires: 1. Knowing the PID of the lock holder 2. A way to check if that process is still alive
3. A way to atomically break the stale lock without corruption.

On Unix/macOS, ``kill(pid, 0)`` checks process liveness and ``rename()`` atomically replaces the lock file. On Windows,
process liveness can be checked via ``OpenProcess``, but the lock file cannot be atomically renamed while another process
holds a handle to it. So stale detection is skipped on Windows to avoid corruption.

Why is ReadWriteLock backed by SQLite?
======================================

A simple file can only track "locked" or "not locked." To track multiple readers + one writer, you need state
management. SQLite handles: - Atomic transactions - Multiple concurrent readers - Exclusive write transactions -
Persistence across process crashes

This makes it the natural choice for read-write locks without adding network dependencies.

****************************
 File permissions and mode
****************************

By default, filelock does not set explicit permissions on the lock file (``mode=-1``). This lets the OS control
permissions through umask and default ACLs. In shared directories with POSIX default ACLs, this preserves ACL
inheritance so the lock file gets the directory's default permissions rather than the creating user's umask.

When you pass an explicit ``mode`` value (e.g., ``mode=0o644``), filelock uses that value directly via ``os.open``. This
overrides any default ACLs on the directory.

*****************************
 Thread-local vs shared state
*****************************

Each ``FileLock`` instance tracks its lock counter and file descriptor in a context object. By default
(``thread_local=True``), each thread gets its own context via ``threading.local``. This means:

- Two threads holding the same ``FileLock`` object each maintain independent lock counters.
- Thread A releasing the lock doesn't affect Thread B's counter.

When ``thread_local=False``, all threads share the same context. This is useful for objects passed between threads, but
requires external coordination to avoid counter mismatches.

Async locks default to ``thread_local=False`` because the thread that calls ``acquire()`` (via
``run_in_executor``) may differ from the thread that calls ``release()``. Using ``thread_local=True`` with
``run_in_executor=True`` raises ``ValueError``.

.. mermaid::

    flowchart LR
        subgraph "thread_local=True (default)"
            T1["Thread A<br/>counter: 2"] --- L1["Lock File"]
            T2["Thread B<br/>counter: 1"] --- L1
        end

        subgraph "thread_local=False"
            T3["Thread A"] --- SC["Shared<br/>counter: 3"] --- L2["Lock File"]
            T4["Thread B"] --- SC
        end

****************************
 When not to use file locks
****************************

**High-frequency synchronization**
    File locks have high latency and are inefficient for protecting variables that change frequently. Use threading
    locks or multiprocessing synchronization primitives instead.

**Distributed systems without shared filesystems**
    If processes are on different machines without a shared filesystem, file locks don't work. Use a centralized lock
    service (Redis, Consul, Zookeeper, etc.).

**Real-time systems**
    File locking is subject to filesystem delays and network latency. Real-time systems need sub-millisecond guarantees
    that file locks can't provide.

**Very large numbers of locks**
    Creating thousands of lock files consumes filesystem resources. For that scale, use an in-memory lock service.

************
 Next steps
************

- New to file locking? Start with :doc:`tutorials`.
- Need to cancel acquisition or force-release? See :ref:`how-to:Cancel lock acquisition` and
  :ref:`how-to:Force-release a lock`.
- Using async locks? See :ref:`how-to:Use async locks`.
- Consult :doc:`api` for complete API documentation.
