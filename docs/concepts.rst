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

This would deadlock forever because ``lock_b`` waits for a lock that ``lock_a`` holds in the same thread.
filelock detects this and raises ``RuntimeError`` with a message suggesting ``is_singleton=True`` as the fix.

This detection only applies to blocking acquires (``timeout < 0``) within the same thread. Non-blocking or timed
acquires raise :class:`Timeout <filelock.Timeout>` as usual.

************************
 How file locking works
************************

File locking takes two approaches:

**OS-level locking** (FileLock on Windows/Unix, UnixFileLock, WindowsFileLock)

The operating system tracks locks on open handles and releases them when a process exits. These locks coordinate code
that follows the same locking convention; they do not prevent unrelated access to the protected resource.

.. list-table::
    :header-rows: 1

    - - Pros
      - Cons
    - - ✓ Enforced by the kernel.
      - ✗ Exclusive only, with no reader/writer distinction (use ReadWriteLock for that).
    - - ✓ Works even if your process crashes.
      - ✗ Unreliable on some network filesystems.
    - - ✓ Lower overhead.
      -

**Soft locks / PID locks** (SoftFileLock, the fallback on systems without fcntl)

A shared marker indicates that a resource is in use. The marker contains the PID and hostname of the holder. A process
attempts acquisition with ``O_CREAT | O_EXCL`` and releases by deleting the pathname. The filesystem must provide
coherent exclusive creation and directory updates to every participant.

This is the same concept as a traditional **PID lock file** (as used by Unix daemons and the deprecated
`lockfile <https://pypi.org/project/lockfile/>`_ library's ``PIDLockFile``). The PID stored in the file identifies the
lock holder via the :attr:`~filelock.SoftFileLock.pid` property and lets a waiter detect stale locks when the holding
process has died.

Same-host contenders can check whether the recorded PID exists. Windows also records process creation time to
distinguish a reused PID. Other platforms can mistake a reused PID for the original holder and remain blocked.

**Strict soft locks** (:class:`StrictSoftFileLock <filelock.StrictSoftFileLock>`) use immutable owner claims instead of
replacing one shared marker. Each contender writes a complete private record, publishes a unique intent through a hard
link, and publishes a unique held claim only after it wins the intent order. The winner rescans before entering. Release
removes its held claim by name, so it cannot detach or delete a successor at another pathname.

.. versionadded:: 3.30.0

The protocol keeps ``<path>`` as a permanent compatibility sentinel and stores claims under
``<path>.filelock/claims``. A legacy :class:`SoftFileLock <filelock.SoftFileLock>` holder can finish before the first
strict acquisition. Once strict mode activates the path, the sentinel prevents legacy clients from entering again.
Every strict participant needs filelock 3.30.0 or newer. Legacy clients configured with ``lifetime``, callers of
``break_lock()``, and code that deletes lock files can remove the sentinel; mixing them with strict clients voids mutual
exclusion.

Strict claims require coherent directory reads and atomic no-replace hard links. The class reports a protocol error
when the filesystem reports that it cannot publish hard links. NFS and SMB remain outside this guarantee until their
mount and server combination passes the protocol tests. Linux documents the hard-link lock pattern in `open(2)
<https://man7.org/linux/man-pages/man2/open.2.html>`_; Microsoft documents NTFS hard links in `Hard Links and Junctions
<https://learn.microsoft.com/en-us/windows/win32/fileio/hard-links-and-junctions>`_.

Strict locks do not infer that an owner died. An orphaned, damaged, or newer-version claim blocks entry. filelock
therefore avoids overlapping a paused holder at the cost of operator recovery after a crash. See
:ref:`how-to:Use fail-closed soft locks` for inspection and recovery.

Private publication records are not ownership claims. Each attempt uses a fresh random name. Linked private names are
discarded immediately, while unlinked records left by a crash receive a two-second grace period before reclamation.
Losing a private name makes the publisher retry; it never converts an unpublished record into ownership.

.. list-table::
    :header-rows: 1

    - - Pros
      - Cons
    - - ✓ Does not require a native file-locking API.
      - ✗ Not enforced; it requires cooperation.
    - - ✓ Uses filesystem operations available on supported platforms.
      - ✗ Higher overhead than OS-level locks.
    - - ✓ Can inspect same-host PID records.
      - ✗ Network filesystems require deployment-specific tests for creation and cache coherence.

***************************
 Platform-specific details
***************************

**Windows**
    Uses the :class:`WindowsFileLock <filelock.WindowsFileLock>` class, backed by ``LockFileEx``/``UnlockFileEx`` over a
    one-byte range on a handle opened with ``NtCreateFile``. Processes using the same byte-range convention contend.

    The lock is exclusive and works reliably on local filesystems. Network filesystem (SMB) support is available but
    considered less reliable.

    Lock file cleanup: Windows attempts to delete the lock file after release, but deletion is not guaranteed in
    multi-threaded scenarios. Windows cannot delete files with open handles, so if another thread acquires the lock
    before the previous holder finishes cleanup, the lock file persists. It does not affect lock correctness. To keep a
    stable pathname across releases, construct the lock with ``preserve_lock_file=True`` (see :ref:`how-to:Keep the lock
    file on release`).

**Unix and macOS**
    Uses the :class:`UnixFileLock <filelock.UnixFileLock>` class, backed by the kernel's ``fcntl.flock`` interface. The
    interface is common on Unix systems but is not specified by POSIX. The lock is exclusive and enforced by the kernel.

    Works best on local filesystems. Network filesystems (NFS) may have issues; locking isn't always reliable on NFS.

    Lock file cleanup: the native lock file remains after release. A persistent empty file does not indicate ownership.
    Keeping one pathname prevents contenders from coordinating through different inodes, which would break mutual
    exclusion; remove the file only after the complete lock protocol has stopped using it.

**Other platforms without fcntl**
    Falls back to :class:`SoftFileLock <filelock.SoftFileLock>` and emits a warning. The lock is not enforced by the OS,
    but filelock includes stale lock detection (though without fcntl, this detection is less reliable than on systems
    with full fcntl support).

*******************************
 Which lock type should I use?
*******************************

**Start with FileLock**, the platform-aware alias that automatically chooses the best backend:

.. code-block:: python

    from filelock import FileLock

    lock = FileLock("work.lock")

This gives you OS-level locking on Windows and Unix/macOS, with a soft-lock alias on builds without ``fcntl``. To
require the Unix backend and reject its runtime ``ENOSYS`` fallback, construct
``UnixFileLock(..., fallback_to_soft=False)`` explicitly. The option cannot change which class ``FileLock`` aliases
when ``fcntl`` is unavailable.

**Use UnixFileLock or WindowsFileLock** when the application requires that backend or its platform-specific behavior.

**Use SoftFileLock** when:

- The filesystem lacks a usable native lock, and operators have verified exclusive creation and cache behavior.
- Cooperating processes can tolerate shared-marker recovery and its overlap risks.

**Use ReadWriteLock** when:

- Your workload is read-heavy with occasional writes.
- Multiple processes need to read simultaneously.
- You want a single writer while no readers are active.
- The lock file lives on a **local** filesystem. ``ReadWriteLock`` is SQLite-backed and is unsafe on NFS.

**Use SoftReadWriteLock** when:

- You need reader/writer semantics on a tested shared filesystem.
- Callers can stop protected work when heartbeat expiry permits another holder to enter.

Lock selection flowchart:

.. mermaid::

    flowchart TD
        start["Choose a lock type"] --> question1{"Read-heavy workload?"}
        question1 -->|Yes| questionRwNet{"Network<br/>filesystem?"}
        questionRwNet -->|Yes| questionRwFs{"Verified marker and<br/>cache behavior?"}
        questionRwFs -->|Yes| srw["Use SoftReadWriteLock"]
        questionRwFs -->|No| service["Use a lock service"]
        questionRwNet -->|No| questionAsync{"Async code?"}
        questionAsync -->|Yes| arw["Use AsyncReadWriteLock"]
        questionAsync -->|No| rw["Use ReadWriteLock"]
        question1 -->|No| question2{"Need network<br/>filesystem support?"}
        question2 -->|Yes| questionSoftFs{"Verified exclusive create<br/>and cache behavior?"}
        questionSoftFs -->|Yes| soft["Use SoftFileLock"]
        questionSoftFs -->|No| service
        question2 -->|No| question3{"Need platform<br/>specific control?"}
        question3 -->|Yes| platform["Use UnixFileLock<br/>or WindowsFileLock"]
        question3 -->|No| default["Use FileLock<br/>(recommended)"]

        classDef recommended fill:#dcfce7,stroke:#22c55e,stroke-width:2px,color:#14532d
        classDef alternative fill:#fef3c7,stroke:#f59e0b,stroke-width:2px,color:#78350f
        classDef special fill:#dbeafe,stroke:#3b82f6,stroke-width:2px,color:#1e3a5f
        class default recommended
        class soft,platform,srw alternative
        class rw,arw special

Lock types compared
===================

.. list-table::
    :header-rows: 1

    - - Feature
      - FileLock
      - SoftFileLock
      - ReadWriteLock
      - SoftReadWriteLock
    - - Exclusive/shared
      - Exclusive only
      - Exclusive only
      - Both (separate context managers)
      - Both (separate context managers)
    - - Platform enforcement
      - OS-level (Windows/Unix)
      - No (file-based)
      - File-based (SQLite)
      - No (file-based, ``O_CREAT|O_EXCL|O_NOFOLLOW``)
    - - Network filesystem
      - Not reliable
      - Works (if you accept the limitations)
      - Not reliable
      - Works, including cross-host on multi-node clusters
    - - Stale lock detection
      - N/A (OS-enforced)
      - Yes (same-host, all platforms)
      - N/A
      - Yes, TTL-based heartbeat (cross-host)
    - - PID inspection
      - No
      - Yes (``pid``, ``is_lock_held_by_us``)
      - No
      - No (content is not a public API)
    - - Lifetime expiration
      - No (ignored with a warning)
      - Yes
      - No
      - Yes (``heartbeat_interval`` / ``stale_threshold``)
    - - Cancel acquisition
      - Yes (``cancel_check``)
      - Yes (``cancel_check``)
      - No
      - No
    - - Force release
      - Yes (``force=True``)
      - Yes (``force=True``)
      - Yes (``force=True``)
      - Yes (``force=True``)
    - - Async support
      - AsyncFileLock
      - AsyncSoftFileLock
      - AsyncReadWriteLock
      - AsyncSoftReadWriteLock
    - - Singleton default
      - No
      - No
      - Yes
      - Yes
    - - Overhead
      - Low
      - High
      - Medium (SQLite)
      - Medium (daemon heartbeat thread + dirfd scans)

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
    NFS and SMB behavior depends on protocol versions, mount options, servers, and client caching. filelock does not
    promise one backend across those configurations. Use a soft lock only after verifying exclusive creation, rename,
    unlink, timestamps, and cache visibility on the target mount. Keep SQLite-backed ``ReadWriteLock`` on a local
    filesystem supported by the active SQLite VFS.

**Locks across different machines**
    Separate machines need a central lock service or a shared filesystem with a tested contract. A shared pathname
    alone does not establish cross-host correctness.

**Read-write semantics**
    FileLock is exclusive only; readers block writers and vice versa. If you need multiple readers with occasional
    writers, use ReadWriteLock instead.

**Atomicity without locks**
    If another process doesn't use the lock, it will still interfere:

    .. code-block:: python

        # Process A (uses lock)
        with lock:
            # Process B might still write at the same time
            # if Process B doesn't use the lock
            open("data.txt").write("A")

    Locks require cooperation; all code accessing a resource must use the same lock.

**************************************
 Trust boundaries and ownership scope
**************************************

Every lock in filelock coordinates *cooperating* participants that agree to use the same lock for the same resource. None
of them deny direct access to the protected resource, and none of them defend against a process that refuses to
cooperate. This section states, for each public lock, what it coordinates, what owns the claim, and what survives
``fork()``, descriptor duplication, task cancellation, and process exit.

The same-UID boundary
=====================

filelock defends one UID's cooperating processes against each other's timing, not against a hostile peer. A process
running under the same effective UID as the holder can read, rewrite, or delete any lock file or marker directly,
whatever its mode bits say. ``0o600`` and ``0o700`` keep *other* UIDs out; they do not make a same-UID co-tenant safe,
because that co-tenant owns the files and can bypass the protocol. Treat every same-UID process on the host as trusted to
follow the protocol. Where that assumption does not hold, a file lock is the wrong tool; use an OS mechanism that enforces
access, such as a privilege boundary or a broker process.

Native locks are advisory
==========================

:class:`FileLock <filelock.FileLock>`, :class:`UnixFileLock <filelock.UnixFileLock>`, and :class:`WindowsFileLock
<filelock.WindowsFileLock>` place an OS lock on an open file. The lock is *advisory*: it blocks another acquirer that
calls the same lock, and it does nothing to a process that opens and writes the protected file without locking. On Unix
the lock lives on the open file description (``flock``), so a ``fork`` or ``dup`` shares one lock and it releases when the
last descriptor sharing that description closes or the process exits. Java's ``FileLock`` draws the same advisory
boundary and tells callers to hold one channel per file; because filelock's Unix backend locks the open file description
rather than the POSIX ``fcntl`` process owner, a second ``open`` of the same path in the same process does not silently
drop the lock, but a caller passing its own descriptor through :func:`lock_descriptor <filelock.lock_descriptor>` still
owns keeping one lock per descriptor.

Ownership scope per lock
========================

.. list-table::
   :header-rows: 1
   :widths: 24 30 46

   * - Lock
     - Owns the claim
     - Survives / releases
   * - :class:`FileLock <filelock.FileLock>` and the native backends
     - The acquisition object, per open file description
     - A forked child or a duplicated descriptor shares the same open file description and its one OS lock; unlocking, or
       the last shared descriptor closing or exiting, releases it for both.
   * - :class:`SoftFileLock <filelock.SoftFileLock>`
     - The acquisition object; the marker names the holder's PID, host, and process start token
     - A crash leaves the marker; a contender reclaims it only once the recorded owner is provably gone. A forked child
       does not hold the lock. Release unlinks the marker.
   * - :class:`StrictSoftFileLock <filelock.StrictSoftFileLock>`
     - The acquisition object, through an owner-specific claim
     - Never reclaims a claim on its own: a crashed holder's claim persists until an operator calls
       :meth:`force_break <filelock.StrictSoftFileLock.force_break>`. This is the fail-closed contract.
   * - :class:`SoftFileLease <filelock.SoftFileLease>`
     - A claim that *expires*; a peer may take it while the previous holder still runs
     - A heartbeat refreshes the claim; ``on_compromise`` fires when it is lost. Overlap is permitted by design, so a
       lease is a hint about who *should* work, not a guarantee that only one worker does.
   * - :class:`ReadWriteLock <filelock.ReadWriteLock>`
     - The acquisition object; state lives in a local SQLite database
     - Requires a local filesystem the SQLite VFS supports. A forked child must build a fresh instance. Crash recovery
       comes from SQLite's transactions.
   * - :class:`SoftReadWriteLock <filelock.SoftReadWriteLock>`
     - The acquisition object; a marker tree with a per-holder heartbeat
     - A peer evicts a marker whose ``mtime`` has not advanced within ``stale_threshold``. A forked child loses the
       lock; the inherited instance is fork-invalidated and its ``release()`` is a no-op.

Task cancellation
=================

The async wrappers (:class:`AsyncFileLock <filelock.AsyncFileLock>` and the async soft, strict, lease, and read-write
variants) run each blocking step on a worker thread. Cancelling the awaiting task while an acquisition is in flight rolls
the attempt back atomically, so a cancelled ``acquire`` never leaves a half-held lock. A task holding a lock that is
cancelled still owns releasing it; cancellation does not release the lock for you.

Strict claims, leases, and fencing
==================================

A :class:`StrictSoftFileLock <filelock.StrictSoftFileLock>` publishes an owner-specific claim: each holder writes its own
claim file and removes only the claims it published, so no holder can detach a peer and no stale break can delete a live
successor. :attr:`claims <filelock.StrictSoftFileLock.claims>` names the published owners, and
:meth:`force_break <filelock.StrictSoftFileLock.force_break>` names the exact claim it removes. Because it never breaks a
claim by age, a strict lock needs no liveness guess; a claim it cannot read without risking overlap raises
:class:`SoftFileLockProtocolError <filelock.SoftFileLockProtocolError>` rather than reclaiming it.

A :class:`SoftFileLease <filelock.SoftFileLease>` trades exclusion for progress. Its :attr:`token
<filelock.SoftFileLease.token>` names the claim this process published; it does **not** fence the protected resource.
Reporting a compromise tells the previous holder to stop, but it cannot stop a holder that paused past its expiry and
resumes afterwards. For true mutual exclusion where an expired holder can still write, the protected resource must be
linearizable and must reject any operation carrying a fencing generation lower than the highest it has accepted, the way
Chubby's lock sequence number and ZooKeeper's ``zxid`` are checked by the resource, not the client. A token names a
claim; only a generation the resource validates can fence one.

Malformed and legacy markers
============================

A soft lock treats a record it cannot parse as malformed, not as a holder. A plain :class:`SoftFileLock
<filelock.SoftFileLock>` self-heals a malformed marker once it ages past a short grace window, which absorbs the brief
gap between creating a marker and writing its record. A strict lock never age-breaks, so it fails closed on an
unreadable claim. A marker written by an older filelock stays readable: the process start token is an integer, so a 3.29
reader parses a newer marker as well-formed and stays conservative rather than evicting a live holder.

Filesystem support matrix
=========================

The contracts above assume the filesystem provides coherent exclusive creation, rename, unlink, timestamps, and cache
visibility to every participating process. The table records where that has been measured.

.. list-table::
   :header-rows: 1
   :widths: 30 20 50

   * - Filesystem
     - Status
     - Notes
   * - Local Linux (ext4, xfs, btrfs, tmpfs)
     - Supported
     - Native and soft backends verified in CI.
   * - Local macOS (APFS)
     - Supported
     - Native and soft backends verified in CI.
   * - Local Windows (NTFS)
     - Supported
     - Native and soft backends verified in CI.
   * - NFS (v3, v4)
     - Unverified
     - POSIX advisory locking is unreliable across NFS implementations, so keep SQLite-backed
       :class:`ReadWriteLock <filelock.ReadWriteLock>` off NFS and verify a soft lock's exclusive creation, rename,
       unlink, timestamps, and cache visibility on the exact mount before relying on it.
   * - SMB / CIFS
     - Unverified
     - Verify the same operations on the target mount and server before use.

Do not read "Unverified" as "broken." It means the project does not yet publish a measured guarantee for that
filesystem. Record the mount and server settings you tested, because cache and locking options change the result.

Migrating from timed stale breaking
===================================

Earlier soft locks broke a marker purely by age: a configured lifetime let any contender remove a marker older than the
lifetime, including while its holder was still running. That is a lease, not mutual exclusion, and it silently permitted
two holders. If you relied on timed breaking to recover from crashes while believing it guaranteed exclusion, move to one
of the explicit contracts: :class:`StrictSoftFileLock <filelock.StrictSoftFileLock>` for real exclusion with an operator
break for crash recovery, or :class:`SoftFileLease <filelock.SoftFileLease>` when overlap is acceptable and you fence the
protected resource. The age-based ``lifetime`` on :class:`SoftFileLock <filelock.SoftFileLock>` remains for backward
compatibility and warns at construction.

*******************
 Design trade-offs
*******************

Why not use OS-level locks everywhere?
======================================

OS-level locks are fast and enforced by the kernel. Their network-filesystem behavior depends on the server, protocol,
mount, and client. ``SoftFileLock`` provides a cooperative fallback when its filesystem operations have been verified.

Why does SoftFileLock use a separate file instead of just checking a directory?
===============================================================================

A directory can't reliably be atomically created and deleted across platforms. A file can be created with ``O_EXCL``
(atomic, all-or-nothing) to detect conflicts. This is why SoftFileLock uses files.

How does stale lock detection work across platforms?
====================================================

Every platform records the holder's PID, hostname, and a process start token in the marker, and a contender reclaims it
only when the recorded owner is provably gone. A PID that no longer exists is gone; a live PID whose start token differs
is a recycled PID, so the process that wrote the marker is gone; a live PID whose token still matches, a token that
cannot be read, and a marker from another host all read as held. The start token is ``kill(pid, 0)`` plus the
``starttime`` from ``/proc/<pid>/stat`` folded with the boot id on Linux, ``sysctl`` process start time on macOS, and the
``GetProcessTimes`` creation time on Windows. A malformed or unparsable record follows a separate rule: a waiter may
remove it after two seconds, so a half-written marker never wedges acquisition. This is the rule PostgreSQL, Qt
``QLockFile``, and Mercurial converge on: break a stale lock only on proof of death.

Why is ReadWriteLock backed by SQLite?
======================================

A simple file can only track "locked" or "not locked." To track multiple readers + one writer, you need state
management. SQLite handles: - Atomic transactions - Multiple concurrent readers - Exclusive write transactions -
Persistence across process crashes

This makes it the natural choice for read-write locks without adding network dependencies.

The async variant (:class:`AsyncReadWriteLock <filelock.AsyncReadWriteLock>`) wraps the same SQLite-backed
implementation. Because Python's :mod:`sqlite3` module has no async API, all blocking operations are dispatched to a
thread pool via ``loop.run_in_executor``. This is the same approach used by :class:`BaseAsyncFileLock
<filelock.BaseAsyncFileLock>`.

How does SoftReadWriteLock work on shared filesystems?
======================================================

:class:`SoftReadWriteLock <filelock.SoftReadWriteLock>` uses a marker directory tree. Deploy it on a shared filesystem
only after verifying its required operations and cache behavior:

- ``<path>.state`` is a short-held :class:`SoftFileLock <filelock.SoftFileLock>` used as the state mutex during
  transitions.
- ``<path>.write`` is the writer marker; its presence blocks readers and other writers.
- ``<path>.readers/<host>.<pid>.<uuid>`` is one marker file per active reader.

Each marker stores a random 128-bit token, the holder's pid, and the holder's hostname. Every acquire uses
``O_CREAT | O_EXCL | O_NOFOLLOW`` with mode ``0o600``; the readers directory uses mode ``0o700`` and a ``lstat``
check plus a dirfd-relative open to close symlink races (which ``mkdir`` alone cannot).

Writer acquisition is two-phase and writer-preferring: phase one atomically claims ``<path>.write`` (which blocks
any new reader as soon as it exists), phase two polls the ``readers/`` directory until every reader has exited.
New readers wait behind an observed writer marker, which gives writers preference among cooperating participants.

Cross-host stale detection
==========================

On a multi-node cluster, a process on ``node-42`` that crashes while holding a lock cannot be detected via
``kill(pid, 0)`` from ``node-17``; the pid means nothing to a different kernel. ``SoftReadWriteLock`` therefore
uses a **TTL with a heartbeat** rather than ``SoftFileLock``'s PID-alive check:

- Each lock instance starts a daemon thread on acquire. The thread refreshes the marker's ``mtime`` every
  ``heartbeat_interval`` seconds (default 30 s).
- A peer may evict a marker whose ``mtime`` has not advanced in ``stale_threshold`` seconds (default 90 s).
- Eviction is atomic: read → rename to a unique ``.break.<pid>.<nonce>`` file → re-verify token and mtime →
  unlink. On verification failure the ``.break.*`` file stays for TTL or atexit cleanup; rollback-rename is itself
  racy and is not attempted.
- The heartbeat thread stops itself on token mismatch or a vanished marker, so a replaced or evicted marker
  never gets accidentally refreshed.

The trade-off: ``stale_threshold`` must be larger than any realistic pause a holder might hit (GC, syscall delay,
filesystem delay). Pick it generously, synchronize participating clocks, and fence protected writes if an expired
holder can resume.

Fork semantics
==============

Python threads do not survive ``fork()``. A process that forks while holding a ``SoftReadWriteLock`` would leave
the child with the marker files, the lock-level state, and no heartbeat thread; the parent would keep
refreshing while the child would not, and both would believe they hold the lock. ``SoftReadWriteLock`` registers
an ``os.register_at_fork(after_in_child=...)`` hook that replaces the inherited ``threading.Lock`` objects with
fresh ones and marks the instance fork-invalidated. ``release()`` on an invalidated instance is a no-op, so an
inherited ``with lock.read_lock():`` block can unwind in the child without raising. The child must construct a
fresh ``SoftReadWriteLock(path)`` before it can acquire again. This matches PyMongo's connection-pool semantics.

**Trust boundary.** The class coordinates cooperating processes. Directory ownership, ACLs, and ``0o600`` / ``0o700``
permissions can exclude another UID. A process with the same effective UID can alter the markers. Clock errors and
filesystem cache behavior can also trigger expiry while a holder remains active.

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

Each ``FileLock`` instance tracks its lock counter, file descriptor, and
configured defaults (``poll_interval``, ``timeout``, ``blocking``, ``mode``,
``lifetime``) in a single context object. By default (``thread_local=True``),
each thread gets its own context via ``threading.local``. This means:

- Two threads holding the same ``FileLock`` object each maintain
  independent lock counters.
- Thread A releasing the lock doesn't affect Thread B's counter.
- Setting a configuration property (for example ``lock.poll_interval = 0.5``)
  affects only the thread that performed the write. Other threads continue
  to see the value supplied to the lock's constructor; ``threading.local``
  re-applies the original constructor arguments the first time each new
  thread accesses the context.

When ``thread_local=False``, all threads share the same context, including
configuration values. This is useful for objects passed between threads or
for cases where you want a property setter to affect all threads, but it
requires external coordination to avoid lock-counter mismatches.

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
