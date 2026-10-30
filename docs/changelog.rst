###########
 Changelog
###########

.. towncrier-draft-entries:: Unreleased

.. towncrier release notes start

.. _release-4-2-5:

********************
 4.2.5 (2026-10-30)
********************

.. _release-4-2-5-fixed:

Fixed
=====

- :meth:`~filelock.BaseFileLock.acquire` and :meth:`~filelock.BaseAsyncFileLock.acquire` raise :class:`TypeError` for a
  *blocking* that is not a :class:`bool`, as the constructor and the ``blocking`` setter already do. A truthy value such
  as the string ``"false"`` used to count as ``True``, so a call meant as a single attempt waited for the instance's
  ``timeout``, and without a limit at the default ``-1`` (:pr:`870`) by :user:`simpleqt` and :user:`gaborbernat`.
- A :class:`~filelock.SoftFileLock` acquisition whose first read of its new marker's identity failed reads it again
  before rolling back. On Windows the rollback could otherwise not recognize the marker it had created, and the empty
  marker it left blocked the path for every later acquisition, since no contender breaks an empty marker (:pr:`869`) by
  :user:`simpleqt` and :user:`gaborbernat`.
- A ``fork()`` child of a process holding a :class:`~filelock.SoftFileLease` starts from an unheld claim. It used to
  inherit the parent's token, last compromise and heartbeat, so :attr:`~filelock.SoftFileLease.token` and
  :attr:`~filelock.SoftFileLease.compromise` reported a claim the child never held while ``is_locked`` reported it
  unheld (:pr:`868`) by :user:`dxbjavid` and :user:`gaborbernat`.
- A nested acquire of a held :class:`~filelock.FileLock` or :class:`~filelock.SoftFileLock` no longer creates the lock
  file's parent directory. After a rename of that directory during the hold, the nested acquire recreated the old name,
  and when a file had taken the name it raised :class:`FileExistsError` and left the hold's recursion count incremented
  (:pr:`878`) by :user:`0x5t4l1n` and :user:`gaborbernat`.
- Fix :class:`ReadWriteLock <filelock.ReadWriteLock>` failing every acquire with "link directory ... was replaced while
  opening" in a group-writable directory whose inherited ACL, such as an NFSv4 one on ZFS, opens the link directory to
  the group. The lock now closes the empty link directory to its owner, and where the filesystem refuses that, such as
  ZFS with ``aclmode=restricted``, it names the directory to make writable by its owner only (:pr:`898`) by
  :user:`gaborbernat`.
- Fix :class:`~filelock.SoftFileLock`, :class:`~filelock.MarkerSoftFileLock` and :class:`~filelock.SoftFileLease`
  raising ``OverflowError`` out of ``acquire()``, or never reclaiming a dead holder's lock, when a stranded restore
  claim beside the lock names a process number outside ``1..2**31-1`` or spells it in non-ASCII digits. Such a claim is
  now ignored, as no filelock writes one (:pr:`871`) by :user:`dxbjavid` and :user:`gaborbernat`.
- Fix soft locks on Windows keeping the marker of a holder that has exited while another process still holds a handle to
  it, such as a parent's ``subprocess.Popen`` after ``wait()`` or ``multiprocessing.Process`` after ``join()``.
  Acquiring such a lock with the default ``timeout=-1`` waited forever (:pr:`874`) by :user:`nk-batman21`.
- Fix the read-only reopen of a lock file this account cannot write resolving a relative path against the working
  directory of the moment. A ``chdir`` between the refused ``O_RDWR`` open and the reopen made the acquisition wait on a
  file that is not there, or probe one in another directory. The reopen now uses the path the acquisition pinned
  (:pr:`889`) by :user:`dxbjavid` and :user:`gaborbernat`.
- Handle ``EMLINK``, the error FreeBSD reports when a no-follow open refuses a symlink, in a soft reader/writer lock's
  protocol tree, so heartbeat reports lost ownership there rather than a transient refresh error (:pr:`866`) by
  :user:`pacocartones` and :user:`gaborbernat`.
- Resolving :class:`~filelock.ReadWriteLock` or :class:`~filelock.AsyncReadWriteLock` raises the :exc:`ImportError` when
  a module other than Python's :mod:`sqlite3` is missing, as in a frozen application that left out a filelock submodule.
  The names are ``None`` only without SQLite (:pr:`880`) by :user:`pintu544` and :user:`gaborbernat`.

.. _release-4-2-5-documentation:

Documentation
=============

- The lock-probing recipe in the how-to guide probes with ``blocking=False`` instead of a tiny ``timeout`` (:pr:`873`)
  by :user:`tongwentao` and :user:`gaborbernat`.

.. _release-4-2-5-packaging:

Packaging
=========

- Releases now run on the shared tox-dev/github-workflows pipeline, with a locked release toolchain and generated GitHub
  Release notes (:pr:`885`) by :user:`gaborbernat`.

.. _release-4-2-4:

********************
 4.2.4 (2026-10-19)
********************

.. _release-4-2-3:

********************
 4.2.3 (2026-10-19)
********************

.. _release-4-2-3-fixed:

Fixed
=====

- Creating a lock on a Python 3.15 interpreter without ``types.LazyImportType`` no longer triggers ``AttributeError`` at
  exit. :pr:`861` - by :user:`gaborbernat`.
- Handle ``EFTYPE`` when a no-follow open refuses a symlink in a soft reader/writer lock's protocol tree, so heartbeat
  reports lost ownership rather than a transient refresh error. :pr:`859` - by :user:`edsonsims`.

.. _release-4-2-2:

********************
 4.2.2 (2026-10-18)
********************

.. _release-4-2-2-fixed:

Fixed
=====

- Fix a lock on a relative path that stayed held after a later ``with lock:`` ended, when an earlier ``acquire()`` had
  failed because the working directory no longer exists. That call still raises :class:`FileNotFoundError`, and it now
  leaves the lock's hold count unchanged. :pr:`856` - by :user:`spicysharkco`.

.. _release-4-2-1:

********************
 4.2.1 (2026-10-17)
********************

.. _release-4-2-1-fixed:

Fixed
=====

- On a singleton wrapper, :meth:`AsyncReadWriteLock.close() <filelock.AsyncReadWriteLock.close>` and
  :meth:`AsyncSoftReadWriteLock.close() <filelock.AsyncSoftReadWriteLock.close>` end only the holds that tasks acquired
  through that wrapper. A reader that arrives during ``close()`` gets the closed error instead of joining the ending
  hold. After synchronous code closes or force-releases the shared lock, the wrappers refuse to admit readers into the
  ended hold, and ``close()`` on such a wrapper returns. Closing one wrapper no longer ends a task's nested hold taken
  through another wrapper on the same path. :pr:`832` - by :user:`gaborbernat`.
- :class:`~filelock.ReadWriteLock` locks again after a ``release()`` that an exception from a signal handler
  interrupted. The async read-write wrappers stop admitting readers into such a level. The async read-write locks forget
  a hold whose backend release failed after the backend let go, and they free the memory of each acquire that times out
  or is canceled. :class:`~filelock.AsyncFileLock` returns from ``release()`` after a few hundred acquires gave up
  behind a waiting one. An exception from a signal handler delivered while an :class:`~filelock.AsyncFileLock` call
  joins its queue no longer blocks every later call on that lock. :pr:`833` - by :user:`gaborbernat`.
- Fix :class:`~filelock.ReadWriteLock` acting on ``-journal``, ``-wal`` or ``-shm`` files another process puts beside
  its database: a write could land in another file, a reader could get in beside a writer, and an acquire could fail.
  SQLite now opens the database by name only in a directory no other account can write, and through a private hard link
  elsewhere. On Windows the directory's access control list decides. Where hard links are unavailable, keep the database
  in a directory other accounts cannot write, such as one under ``%LOCALAPPDATA%``. The lock removes link directories
  through a checked descriptor rather than by name, reads a link directory's name only when the locking account owns it,
  and leaves alone a directory with other content renamed onto a link directory's name. A database whose schema takes
  more than a few kilobytes to load raises :class:`ValueError` instead of running past the acquire timeout. :pr:`834` -
  by :user:`gaborbernat`.
- Fix five bugs in opening the lock file. :class:`~filelock.UnixFileLock` no longer truncates a file it did not create,
  such as one renamed to the lock path, and no longer applies ``mode`` to it. An ``on_acquired`` hook that writes holder
  metadata now truncates the file itself. A lease (Linux) or an oplock (Windows) on the lock file or a soft lock's
  marker no longer holds ``acquire()`` past its ``timeout`` or ``blocking=False``, and an oplock no longer holds
  ``release()``; :class:`~filelock.ReadWriteLock` on Windows waits for its ``timeout`` and raises
  :class:`~filelock.Timeout`. On Windows, a ``mode`` without group and other bits, such as ``0o600``, now creates the
  lock file with an owner-only permission list; the :class:`~filelock.ReadWriteLock` database gets the same. The soft
  locks' writability check no longer follows a symlink swapped in at the lock path. On a filesystem without ``flock``, a
  contender from another account now waits on a marker it cannot write instead of raising ``PermissionError``. :pr:`835`
  - by :user:`gaborbernat`.
- Fix :class:`~filelock.SoftFileLock`, :class:`~filelock.MarkerSoftFileLock` and :class:`~filelock.SoftFileLease`
  removing a live holder's marker in three cases: the holder and the contender run in Linux time namespaces with
  different boottime offsets; only one of them can read the boot id; a process other than the owner rewrote the marker.
  Soft markers no longer carry group or other write bits. A process name containing a carriage return or another line
  separator no longer makes the stale check raise :class:`ValueError`. When ``/proc/self/ns/time`` exists but cannot be
  read, the stale check no longer compares start times and keeps the marker. :pr:`836` - by :user:`gaborbernat`.
- The async read-write locks record their hold under a task owner, so synchronous code on the executor thread that took
  the hold can no longer re-enter or release it. :class:`~filelock.AsyncSoftReadWriteLock` gives back one level on
  release, so synchronous readers of a shared singleton keep theirs. :class:`~filelock.SoftReadWriteLock` tracks read
  levels per thread: ``release()`` from a thread that holds none raises :class:`RuntimeError`. After its heartbeat
  recorded a lost hold, it refuses new acquires until that hold is released. :pr:`838` - by :user:`gaborbernat`.
- :class:`~filelock.StrictSoftFileLock` creates ``<path>.filelock`` and its ``claims`` directory with the lock's
  ``mode`` and sets the sticky bit (a directory flag that lets only a file's owner remove it) on both. On the next
  acquisition it narrows a directory it owns that an earlier release created wider; it leaves a directory of another
  account as it is, so remove such leftovers by hand. It reaches ``claims`` through a descriptor opened without
  following symlinks; on Windows it holds both directories open and refuses a junction (a directory that points at
  another directory). Under a sticky parent such as ``/tmp`` it refuses a coordination directory of another account, as
  :class:`~filelock.SoftReadWriteLock` does for ``<path>.rw``. One read grace now covers a whole claim scan, a failed
  claim cleanup no longer leaves the sentinel descriptor open, and a non-regular node at a sentinel private-record name
  raises :class:`~filelock.SoftFileLockProtocolError`. On Windows, contenders read claims without blocking their owner's
  unlink. :pr:`839` - by :user:`gaborbernat`.
- Fix a lock reporting a hold the OS had dropped after a signal handler raised during
  :meth:`~filelock.BaseFileLock.acquire` or :meth:`~filelock.BaseFileLock.release`. A release that a signal handler cuts
  short after it has started now completes before the error propagates. A handler that acquires the lock its own thread
  is releasing now gets :class:`RuntimeError` instead of a hold that stays held. The singleton cache no longer hands out
  an ``is_singleton=True`` lock or a :class:`~filelock.ReadWriteLock` whose finalizer is running, which released the new
  holder's lock. A signal during ``os.fork()`` no longer leaves the other threads blocked in their next lock call, and a
  fork from another thread no longer waits for a :class:`~filelock.ReadWriteLock` acquire on a busy database. :pr:`840`
  - by :user:`gaborbernat`.
- Fix a lock on a relative path locking a file in the directory the process changed to while the lock waited, and
  leaving its marker behind on release after such a change. The path resolved when an acquisition starts stays in use
  until the hold ends. An ``is_singleton=True`` lookup no longer returns a cached lock whose relative path now resolves
  to another file; a held lock still matches the file it holds. A rename of the lock's directory during a hold no longer
  leaves the marker behind or reports a false lease compromise, and a waiter no longer creates a second lock file in a
  directory that was renamed away. :pr:`841` - by :user:`gaborbernat`.
- :class:`~filelock.SoftReadWriteLock` reaches its records through descriptors of ``<path>.rw`` and ``gen`` or
  ``holders`` opened without following symlinks. On Windows it holds those directories open and refuses a junction (a
  directory that points at another directory). A tree swapped in after an earlier acquire therefore cannot redirect
  them. Its heartbeat rewrites a holder record only when the name still leads to the file it created, and does not block
  on a FIFO (a named pipe). A heartbeat that finds ``<path>.rw`` replaced by a symlink or junction reports the hold lost
  instead of ending the heartbeat thread. A live lock object repeats the ownership check whenever the tree it opens is
  not the one it prepared. :pr:`842` - by :user:`gaborbernat`.
- Fix :class:`~filelock.SoftFileLock`, :class:`~filelock.MarkerSoftFileLock` and :class:`~filelock.SoftFileLease`
  removing or displacing a live holder's marker in five cases. ext4 gave a freed inode number to the next marker. A
  stalled :class:`~filelock.FileLock` removed its ``ENOSYS`` placeholder after a peer replaced it. A failed acquire
  cleaned up after a successor took the path. A stale break left the lock path empty while it checked the marker it had
  moved. A ``lifetime`` break judged a marker by a reused inode number. On Linux and macOS a stale marker now changes
  hands in one atomic exchange. On other platforms without one, except Windows, including NFS and SMB, the marker stays
  in place and waits for ``timeout`` or ``break_lock()``. Restoring a marker no longer follows a symlink found under the
  break name. A holder that releases while a peer's exchange has its marker aside leaves a record no lock parses, so the
  put-back lands a marker that ages out. A lease heartbeat that finds another file at the lock path once, while a peer's
  break has its marker aside, waits for the next refresh before it reports a compromise. A contender that finds a dead
  breaker's marker puts back the marker that breaker left under its break name. A marker is the breaker's own only when
  it names the breaker's process on this host; the same process number on another host is a holder. A contender takes a
  stranded marker under a name of its own before it puts the marker back, so two contenders no longer reverse each
  other's exchange and remove the live holder's marker. A claim left behind by a failed put-back is retried on the next
  poll. :pr:`843` - by :user:`gaborbernat`.
- Canceling an :class:`~filelock.AsyncReadWriteLock` or :class:`~filelock.AsyncSoftReadWriteLock` acquire that waits for
  another process now stops the wait and raises the cancellation. It used to wait until that process released, or raise
  :class:`~filelock.Timeout`. A ``thread_local=True`` :class:`~filelock.AsyncFileLock` that threads with their own event
  loops share no longer deadlocks when one thread waits for the lock while another holds it. :pr:`844` - by
  :user:`gaborbernat`.
- :class:`~filelock.SoftReadWriteLock` refuses a commit that lands a whole retention window behind the log, so two
  committers stalled at adjacent generations can no longer admit a second writer. Its stale check samples the clock
  before it reads a holder record, so a pause between the two no longer counts as time the record stayed unchanged.
  :pr:`845` - by :user:`gaborbernat`.
- Fix a :class:`~filelock.SoftFileLock` contender evicting an empty marker after two seconds while its owner held the
  lock. The owner is a ``SoftFileLock`` before 3.22, which writes nothing into its marker, or a current one still
  writing its record. An empty marker now reads as held, and ``timeout``, ``lifetime`` or ``break_lock()`` clears it. A
  contender that finds an empty marker a dead breaker left aside puts it back instead of removing it. :pr:`846` - by
  :user:`gaborbernat`.
- Fix :class:`~filelock.ReadWriteLock` on Windows acting on ``-journal`` and ``-wal`` files another account places
  beside its database: an acquire could fail with :class:`sqlite3.OperationalError`, or SQLite could replay the journal
  and delete the file it names. The lock now reads the database directory's access control list. Where an account other
  than the locking account, ``SYSTEM`` and ``Administrators`` can add or remove names, SQLite opens a hard link inside a
  directory only those accounts can write. A filesystem without hard links raises :class:`OSError`. :pr:`854` - by
  :user:`gaborbernat`.
- Fix ``SoftReadWriteLock`` subclasses sharing singleton instances with the base class or sibling subclasses. Each
  concrete class now keeps its own cache and construction guard, including after fork, while preserving cooperative
  class-definition keywords. :pr:`855` - by :user:`AryanRG`.

.. _release-4-2-0:

********************
 4.2.0 (2026-10-14)
********************

.. _release-4-2-0-fixed:

Fixed
=====

- Fix :class:`~filelock.StrictSoftFileLock` raising :class:`~filelock.SoftFileLockProtocolError` on Windows when a
  contender read a claim whose owner was still finishing its unlink. :pr:`829` - by :user:`gaborbernat`.

.. _release-4-2-0-performance:

Performance
===========

- ``import filelock`` is about twice as fast: lock types other than :class:`~filelock.FileLock` load on first use.
  :pr:`831` - by :user:`gaborbernat`.

.. _release-4-1-2:

********************
 4.1.2 (2026-10-14)
********************

.. _release-4-1-2-fixed:

Fixed
=====

- Fix read-lock acquisition on SQLite versions older than 3.33.0 by using ``sqlite_master`` for the schema probe.
  :pr:`827` - by :user:`DawnofGenX`.
- Raise ``IsADirectoryError`` (or ``PermissionError`` on Windows) for a directory lock path regardless of its write
  permissions. :pr:`822` - by :user:`feiiiiii5`.
- Fix a forked child hanging at exit on PyPy when the parent forked while an :class:`~filelock.AsyncReadWriteLock`
  worker thread was between two operations. :pr:`828` - by :user:`gaborbernat`.
- Hash all cache keys in the lock filename recipe so short path-like keys stay in the chosen directory. The example also
  accepts path-derived surrogate escapes and works before the cache directory exists. :pr:`824` - by :user:`xiehuanyi`.

.. _release-4-1-1:

********************
 4.1.1 (2026-10-11)
********************

.. _release-4-1-1-fixed:

Fixed
=====

- Avoid the macOS ``/dev/fd`` lookup implicated in a kernel panic by opening SQLite through a validated hard link. The
  database directory must be writable and support hard links; alias creation failure raises ``OSError``. :pr:`820` - by
  :user:`gaborbernat`.
- Ignore non-ASCII PID or start-time fields and out-of-range PIDs in ``ReadWriteLock`` hard-link directory names so
  forged owners cannot abort acquisition. :pr:`821` - by :user:`dxbjavid`.

.. _release-4-1-0:

********************
 4.1.0 (2026-10-09)
********************

.. _release-4-1-0-added:

Added
=====

- Import :data:`~filelock.OwnerMode`, :data:`~filelock.CompromiseReason` and :data:`~filelock.has_fcntl` from
  ``filelock``. :pr:`790` - by :user:`gaborbernat`.
- :class:`~filelock.AcquireReturnProxy` is generic in the lock type, so ``with lock.acquire() as held:`` types ``held``
  as the lock's own class. :pr:`803` - by :user:`gaborbernat`.
- A :class:`~filelock.Timeout` from a soft lock names a holder on another host or in another PID namespace, which no
  contender ever reclaims. :pr:`815` - by :user:`gaborbernat`.
- Every option given in seconds takes a :class:`~decimal.Decimal` or :class:`~fractions.Fraction`, as a file lock's
  ``timeout`` already did, and a read-write lock or lease refuses a ``bool`` or string one. :pr:`815` - by
  :user:`gaborbernat`.

.. _release-4-1-0-fixed:

Fixed
=====

- :attr:`~filelock.SoftFileLock.is_lock_held_by_us` and :attr:`~filelock.MarkerSoftFileLock.is_lock_held_by_us` return
  ``False`` for a marker an earlier process with the same PID left behind. :pr:`774` - by :user:`lprnmns`.
- :class:`~filelock.SoftFileLock` removes its marker when writing it fails and closing the descriptor fails too, so the
  lock path no longer stays blocked. :pr:`775` - by :user:`harbinresearcher`.
- :meth:`~filelock.BaseFileLock.acquire` and :meth:`~filelock.BaseAsyncFileLock.acquire` no longer wait past ``timeout``
  when ``poll_interval`` is longer than the time left. :pr:`777` - by :user:`feiiiiii5`.
- :class:`~filelock.SoftReadWriteLock` ignores a generation-log entry whose name is not 20 ASCII digits, which used to
  stop its heartbeat thread or delete real generations. :pr:`778` - by :user:`dxbjavid`.
- :class:`~filelock.ReadWriteLock`, :class:`~filelock.SoftReadWriteLock` and their async wrappers treat a
  :data:`math.inf` timeout like ``-1`` and raise :class:`ValueError` for a blocking ``nan`` one. :pr:`779` - by
  :user:`darrenhuai`.
- :class:`~filelock.ReadWriteLock` and :class:`~filelock.SoftReadWriteLock` accept a timeout above
  :data:`threading.TIMEOUT_MAX` instead of raising :class:`OverflowError`. :pr:`780` - by :user:`darrenhuai`.
- :meth:`~filelock.BaseFileLock.acquire` raises :class:`ValueError` for a blocking ``nan`` timeout or a
  ``poll_interval`` above :data:`threading.TIMEOUT_MAX`, and treats a :data:`math.inf` timeout like ``-1``. :pr:`781` -
  by :user:`gaborbernat`.
- :class:`~filelock.SoftFileLock` and :class:`~filelock.MarkerSoftFileLock` treat a marker cut before its final newline
  as malformed, so a crash-truncated record self-heals and a read mid-write no longer breaks a live lock. :pr:`782` - by
  :user:`gaborbernat`.
- :class:`~filelock.UnixFileLock` no longer deletes another process's live marker when it falls back to a soft lock on
  ``ENOSYS``, and on macOS a lock path ending in a separator raises :class:`FileNotFoundError` instead of retrying.
  :pr:`785` - by :user:`gaborbernat`.
- :class:`~filelock.SoftFileLock` and :class:`~filelock.SoftFileLease` put back a live successor's marker that a racing
  stale break moved aside, so a third contender no longer acquires beside the successor. :pr:`786` - by
  :user:`gaborbernat`.
- :class:`~filelock.StrictSoftFileLock` removes every claim a failed acquire published, counts an ``EEXIST`` link that
  landed as published, and no longer reports ``is_locked`` after a failed claim cleanup. :pr:`787` - by
  :user:`gaborbernat`.
- On Python 3.10 without the ``exceptiongroup`` backport, a release failure under the default ``context_error_policy``
  no longer turns into :exc:`ModuleNotFoundError`; cleanup failures chain instead of grouping, and ``on_acquired`` is
  allowed. :pr:`788` - by :user:`gaborbernat`.
- :class:`~filelock.AsyncReadWriteLock` and :class:`~filelock.AsyncSoftReadWriteLock` drop a task's hold when a caller
  cancels it during a release that still completes, instead of blocking every later writer. :pr:`789` - by
  :user:`gaborbernat`.
- :class:`~filelock.SoftFileLease` releases a lease dropped while held, at garbage collection or interpreter exit, and
  raises :class:`ValueError` for a ``heartbeat_interval`` above :data:`threading.TIMEOUT_MAX`. :pr:`791` - by
  :user:`gaborbernat`.
- :class:`~filelock.StrictSoftFileLock` judges an abandoned private record's age by the filesystem's clock, so a client
  clock ahead of an NFS server no longer aborts live publications. :pr:`792` - by :user:`gaborbernat`.
- :class:`~filelock.BaseAsyncFileLock` raises :class:`RuntimeError` instead of :class:`asyncio.CancelledError` when its
  executor shuts down with the call still queued, and a waiter interrupted by anything else no longer wedges later
  calls. :pr:`793` - by :user:`gaborbernat`.
- Constructing or closing a second :class:`~filelock.ReadWriteLock` on a held database no longer drops the holder's
  lock, so other processes stay excluded. :pr:`794` - by :user:`gaborbernat`.
- :class:`~filelock.SoftReadWriteLock` no longer admits a second writer after ``<path>.rw`` is removed under a live
  instance, and flushes each snapshot before publishing it so a crash cannot leave an empty one that wedges every peer.
  :pr:`795` - by :user:`gaborbernat`.
- :class:`~filelock.SoftFileLease` reclaims a dead :class:`~filelock.MarkerSoftFileLock` owner, and expires a lease or a
  malformed soft marker after it stays unchanged on the contender's own monotonic clock, so clock skew cannot break it.
  :pr:`796` - by :user:`gaborbernat`.
- A soft lock marker written in a Linux container records its PID namespace with the hostname, so a sibling container
  that shares the hostname and volume no longer breaks a live lock. :pr:`797` - by :user:`gaborbernat`.
- :meth:`SoftReadWriteLock.close() <filelock.SoftReadWriteLock.close>` makes an acquire still waiting in another thread
  raise :class:`RuntimeError`, where it used to take the lock on the closed instance and hold it until the process
  exited. :pr:`798` - by :user:`gaborbernat`.
- :class:`~filelock.SoftReadWriteLock` reads the head from ``gen/HEAD``, so a client whose cached directory listing is
  more than 64 generations stale no longer takes an old generation for the head and forks the log. :pr:`799` - by
  :user:`gaborbernat`.
- When the garbage collector finalizes a held :class:`~filelock.BaseAsyncFileLock`, it releases on a ``loop`` running in
  another thread, or in place once no loop is left; the ``lifetime`` check and path resolution now run in the executor.
  :pr:`800` - by :user:`gaborbernat`.
- :class:`~filelock.SoftReadWriteLock` waits out a holder's own ``stale_threshold`` before evicting it, keys its
  singleton on the path its log sits beside, and no longer leaks ledger entries or the holder record of a failed
  acquire. :pr:`801` - by :user:`gaborbernat`.
- :func:`~filelock.lock_descriptor` raises :class:`ValueError` for a blocking ``poll_interval`` above
  :data:`threading.TIMEOUT_MAX` up front, instead of :class:`OverflowError` once the descriptor is contended. :pr:`802`
  - by :user:`sricharanreddycheruku`.
- :class:`~filelock.BaseFileLock` rejects a ``bool`` or string timeout, an out-of-range ``mode`` and a non-``bool``
  ``blocking`` set at any time, rejects unknown options on a singleton cache hit, and tracks an ``on_acquired``
  reacquire. :pr:`803` - by :user:`gaborbernat`.
- :class:`~filelock.StrictSoftFileLock` raises :class:`~filelock.SoftFileLockProtocolError` naming a directory, symlink
  or exited soft-lock owner's marker at the lock path, and ``lock_path_occupant`` shows what blocks it. :pr:`804` - by
  :user:`gaborbernat`.
- :class:`~filelock.ReadWriteLock` raises :class:`ValueError` for a WAL-mode database instead of ``disk I/O error``, and
  its ``/dev/fd`` fallback links beside the database, so a lock off the TMPDIR filesystem no longer fails with
  ``EXDEV``. :pr:`805` - by :user:`gaborbernat`.
- :meth:`ReadWriteLock.release() <filelock.ReadWriteLock.release>` from a thread that holds no read level raises
  :class:`RuntimeError` instead of ending another thread's read lock and letting a writer in. :pr:`806` - by
  :user:`gaborbernat`.
- :class:`~filelock.SoftFileLockLifetimeWarning` derives from :class:`FutureWarning` instead of
  :class:`DeprecationWarning`, so Python shows it by default outside ``__main__``. :pr:`807` - by :user:`gaborbernat`.
- :class:`~filelock.SoftFileLock` applies an explicit ``mode`` past the umask as :class:`~filelock.UnixFileLock` does,
  and reads a marker number spelled other than in ASCII digits, or a lease with an empty token, as malformed. :pr:`807`
  - by :user:`gaborbernat`.
- :class:`~filelock.ReadWriteLock` raises :class:`OSError` when its database is unlinked or replaced while an
  acquisition waits, instead of taking a lock that no later opener sees. :pr:`808` - by :user:`gaborbernat`.
- On a thread-local synchronous lock, :meth:`~filelock.BaseFileLock.release` with ``force=True`` and garbage collection
  release holds exited threads left, and a ``release()`` re-entered from a signal handler closes the descriptor once.
  :pr:`810` - by :user:`gaborbernat`.
- :class:`~filelock.SoftFileLease` records a ``lease_duration`` given as a :class:`float` subclass, such as NumPy's, in
  a form every contender parses, so a live lease is no longer evicted as malformed. :pr:`815` - by :user:`gaborbernat`.
- :class:`~filelock.StrictSoftFileLock` raises :class:`~filelock.SoftFileLockProtocolError` for a file at the lock path
  it cannot parse once it stays unchanged for two seconds, instead of waiting on it forever. :pr:`815` - by
  :user:`gaborbernat`.
- A lock finalized at interpreter exit no longer hangs when a daemon thread stopped inside filelock's fork bookkeeping.
  :pr:`815` - by :user:`gaborbernat`.
- Setting :attr:`BaseFileLock.timeout <filelock.BaseFileLock.timeout>` from a string emits a :class:`DeprecationWarning`
  and from a ``bool`` raises :class:`TypeError`; pass the number of seconds instead. :pr:`815` - by :user:`gaborbernat`.
- Every lock raises :class:`ValueError` for a timeout too large for a :class:`float`, and a singleton accepts a timeout
  that waits as long as the cached one, such as ``inf`` for ``-1``. :pr:`815` - by :user:`gaborbernat`.
- On macOS, :class:`~filelock.ReadWriteLock` connects through a private hard link, so cycling a lock beside a holder no
  longer adds an open descriptor per connection. :pr:`815` - by :user:`gaborbernat`.
- :class:`~filelock.SoftFileLock` and :class:`~filelock.SoftFileLease` re-check a stale marker right before breaking it,
  so a marker recreated since the check is no longer moved aside for a third contender to take the path. :pr:`815` - by
  :user:`gaborbernat`.
- An acquire a signal handler starts while its thread is still acquiring the same lock raises :class:`RuntimeError`,
  where it used to hang that thread or leave the lock held after the outer release. :pr:`815` - by :user:`gaborbernat`.
- A :class:`~filelock.BaseAsyncFileLock` singleton the garbage collector finalizes leaves the singleton cache first, so
  a new lock on the path waits for its release instead of re-entering a hold that release then drops. :pr:`815` - by
  :user:`gaborbernat`.
- A finalized thread-local :class:`~filelock.BaseAsyncFileLock` releases every thread's hold, including one on a daemon
  thread's event loop at interpreter exit, instead of leaving it held. :pr:`815` - by :user:`gaborbernat`.
- A soft lock in the host's PID namespace no longer breaks a live marker written from a container sharing the hostname
  while the kernel shows its owner running in another namespace under the recorded PID and start time. :pr:`815` - by
  :user:`gaborbernat`.
- :class:`~filelock.SoftFileLock` and its marker and lease variants no longer leave their marker behind when a signal
  handler interrupts a release; the retried release, or the next acquire on Windows, removes it. :pr:`815` - by
  :user:`gaborbernat`.
- :class:`~filelock.SoftReadWriteLock` raises :class:`ValueError` for a ``heartbeat_interval`` above
  :data:`threading.TIMEOUT_MAX`, which killed its heartbeat thread, and an interrupted release still removes its record.
  :pr:`815` - by :user:`gaborbernat`.
- :class:`~filelock.AsyncReadWriteLock` releases only its own hold on a singleton it shares with synchronous users,
  where it used to end their read transaction too. :pr:`815` - by :user:`gaborbernat`.

.. _release-4-1-0-documentation:

Documentation
=============

- The documentation's canonical link, ``og:url`` and sitemap entries point at the versioned page on Read the Docs
  instead of a domain-root URL that returns 404. :pr:`813` - by :user:`gaborbernat`.

.. _release-4-0-12:

*********************
 4.0.12 (2026-10-05)
*********************

.. _release-4-0-12-fixed:

Fixed
=====

- Fix read-lock acquisition on SQLite versions older than 3.33.0 by using ``sqlite_master`` for the schema probe.
  :pr:`769` - by :user:`Vikram12301`.

.. _release-4-0-11:

*********************
 4.0.11 (2026-10-05)
*********************

.. _release-4-0-11-fixed:

Fixed
=====

- ``ReadWriteLock`` and ``AsyncReadWriteLock`` use a validated private hard link when ``/dev/fd`` has no entry for the
  database descriptor, supporting NetBSD's static descriptor directory beyond descriptor 63. The temporary location must
  share a filesystem with the database. The symlink refusal test accepts NetBSD's error wording - by
  :user:`gaborbernat`.

.. _release-4-0-10:

*********************
 4.0.10 (2026-10-03)
*********************

.. _release-4-0-10-fixed:

Fixed
=====

- Reusing a singleton ``AsyncFileLock`` with another ``loop``, ``executor`` or ``run_in_executor``, or a singleton
  ``SoftFileLease`` with another ``lease_duration``, ``heartbeat_interval`` or ``on_compromise``, now raises
  ``ValueError`` instead of returning the lock with its original options. :pr:`765` - by :user:`kokotatan`.

.. _release-4-0-9:

********************
 4.0.9 (2026-10-01)
********************

.. _release-4-0-9-fixed:

Fixed
=====

- ``ReadWriteLock`` and ``AsyncReadWriteLock`` close the descriptor that checks the database path once SQLite has
  connected, so on PyPy a dropped lock leaves no descriptor open until garbage collection runs. :pr:`763` - by
  :user:`gaborbernat`.

.. _release-4-0-9-security:

Security
========

- ``ReadWriteLock`` and ``AsyncReadWriteLock`` refuse a symlink at the database path instead of following it, so a user
  who can create names in a shared lock directory cannot point the lock at another file (GHSA-j8f7-rjxc-mr56) - by
  :user:`gaborbernat`.

.. _release-4-0-8:

********************
 4.0.8 (2026-10-01)
********************

.. _release-4-0-8-fixed:

Fixed
=====

- ``ReadWriteLock.release()`` and ``SoftReadWriteLock.release()`` from a thread that does not hold the write lock now
  raise ``RuntimeError`` instead of dropping the holder's lock and letting a second writer in. :pr:`761` - by
  :user:`feiiiiii5`.

.. _release-4-0-7:

********************
 4.0.7 (2026-09-29)
********************

.. _release-4-0-7-fixed:

Fixed
=====

- File locks now raise ``ValueError`` at construction when ``mode`` denies the owner read or write, such as
  ``mode=0o444``, instead of failing on a later acquire and staying broken until someone deletes the lock file.
  :pr:`760` - by :user:`gaborbernat`.

.. _release-4-0-6:

********************
 4.0.6 (2026-09-28)
********************

.. _release-4-0-6-fixed:

Fixed
=====

- Reject negative blocking timeouts other than ``-1`` before reentrant ``ReadWriteLock`` and ``SoftReadWriteLock``
  acquisition. Preserve unlimited waits and nonblocking acquisition. :pr:`756` - by :user:`feiiiiii5`.

.. _release-4-0-5:

********************
 4.0.5 (2026-09-28)
********************

.. _release-4-0-5-fixed:

Fixed
=====

- Fix ``MarkerSoftFileLock`` acquisition and prevent contenders from evicting live protocol-2 owners after two seconds.
  Reclaim recognized records after owner death; preserve unknown contracts. :pr:`751` - by :user:`DawnofGenX`.
- Honor instance ``timeout`` and ``blocking`` settings in sync and async ``ReadWriteLock`` acquisition, including waits
  between tasks on one instance. Preserve explicit per-call overrides. :pr:`752` - by :user:`DawnofGenX`.
- Skip access-denial checks when the process can read mode-``0o000`` files. Keep mode-bit checks enabled for privileged
  processes on filesystems that support POSIX permissions. :pr:`753` - by :user:`pasmud`.
- Skip vanished ``StrictSoftFileLock`` claims after a read-permission retry expires. Recheck the directory before
  raising a protocol error so concurrent removal does not turn a stale claim listing into an acquisition failure.
  :pr:`754` - by :user:`januththedev`.
- Reject negative timeouts other than ``-1`` in blocking ``AsyncReadWriteLock`` and ``AsyncSoftReadWriteLock``
  acquisitions. Keep ``-1`` as an unlimited wait and ignore timeouts when ``blocking=False``. :pr:`755` - by
  :user:`pasmud`.

.. _release-4-0-4:

********************
 4.0.4 (2026-09-26)
********************

.. _release-4-0-4-fixed:

Fixed
=====

- Hostnames that still differ after their first 253 escaped characters now publish distinct owners, so a soft lock no
  longer takes another such host's live holder for its own and reclaims its marker. :pr:`748` - by :user:`feiiiiii5`.

.. _release-4-0-3:

********************
 4.0.3 (2026-09-23)
********************

.. _release-4-0-3-fixed:

Fixed
=====

- Importing filelock on CPython 3.10 or 3.11 no longer makes new threads fail with ``RuntimeError: Cannot install a
  trace function while another trace function is being installed`` under coverage or a debugger. filelock skips its
  fork-safety audit hook there, so forking from inside a thread's own lock-state transition no longer raises immediately
  on those versions. :pr:`747` - by :user:`gaborbernat`.

.. _release-4-0-2:

********************
 4.0.2 (2026-09-23)
********************

.. _release-4-0-2-fixed:

Fixed
=====

- Concurrent ``acquire()`` and ``release()`` on a ``thread_local=False`` lock no longer leak the OS lock, close a
  descriptor twice, drop a lease token, or leave a false deadlock after a cross-thread release (:issue:`744`). :pr:`745`
  - by :user:`gaborbernat`.
- :class:`~filelock.AsyncReadWriteLock` and :class:`~filelock.AsyncSoftReadWriteLock` now give each ``asyncio`` task its
  own hold, so tasks sharing one instance no longer enter the write lock together. :pr:`746` - by :user:`gaborbernat`.
- Correct the async cache example to create its data directory and clarify automatic creation of lock-file parent
  directories. :pr:`740` - by :user:`agammann`.

.. _release-4-0-1:

********************
 4.0.1 (2026-09-19)
********************

.. _release-4-0-1-fixed:

Fixed
=====

- ``poll_interval`` is now validated at construction, on the setter, and on ``acquire()``: a negative, non-finite, or
  non-numeric value raises :class:`ValueError`/:class:`TypeError` immediately instead of failing inside ``time.sleep``.
  :pr:`739` - by :user:`Pitchfork-and-Torch`.

.. _release-4-0-0:

********************
 4.0.0 (2026-09-17)
********************

.. _release-4-0-0-added:

Added
=====

- :class:`~filelock.SoftReadWriteLock` exposes :attr:`~filelock.SoftReadWriteLock.generation` as a fencing token for the
  protected resource and reports a lost hold through ``on_compromise`` and
  :attr:`~filelock.SoftReadWriteLock.compromise`. :pr:`738` - by :user:`gaborbernat`.

.. _release-4-0-0-fixed:

Fixed
=====

- The :class:`~filelock.SoftReadWriteLock` on-disk protocol is a generation log under ``<path>.rw``, and a process
  running an earlier release does not see it: an old and a new participant on one lock path do not exclude each other.
  Stop every participant, upgrade them all, then restart them; the new code ignores leftover ``.state``, ``.write`` and
  ``.readers/`` files, and you can delete them. The filesystem must provide no-replace hard links, as it must for
  :class:`~filelock.StrictSoftFileLock`, so a runtime without ``os.link`` raises
  :class:`~filelock.SoftFileLockProtocolError` on acquire. Constructing a singleton again with a different
  ``on_compromise``, or with ``poll_interval`` at or above ``stale_threshold``, now raises :class:`ValueError`.
  :pr:`738` - by :user:`gaborbernat`.
- :class:`~filelock.SoftReadWriteLock` no longer deadlocks when a holder dies on another host mid-transition, and
  ``release()`` no longer waits on a mutex a dead host left behind. The state mutex is gone. Each transition is one
  atomic snapshot commit, and liveness is a heartbeat nonce read on the observer's own clock rather than an ``mtime``
  read against another host's. :pr:`738` - by :user:`gaborbernat`.

.. _release-3-32-7:

*********************
 3.32.7 (2026-09-16)
*********************

.. _release-3-32-7-fixed:

Fixed
=====

- Document that :meth:`~filelock.BaseFileLock.acquire` reads ``blocking=None`` as the lock's ``blocking`` attribute and
  raises :class:`~filelock.Timeout` after one attempt when ``blocking=False``. :pr:`733` - by :user:`hxperl`.

.. _release-3-32-6:

*********************
 3.32.6 (2026-09-08)
*********************

.. _release-3-32-6-added:

Added
=====

- Allow acquiring existing lock files that grant write access through group permissions or an ACL even when their
  owner-write mode bit is unset. :pr:`728` - by :user:`jackwalkerlabs`.

.. _release-3-32-6-fixed:

Fixed
=====

- ``SoftFileLease`` and ``AsyncSoftFileLease`` now reject a boolean or non-finite ``lease_duration``, which used to
  publish an owner record their own ``owner`` property reads back as malformed. :pr:`723` - by :user:`lprnmns`.
- Reject non-finite heartbeat, stale, and polling intervals in ``SoftReadWriteLock`` and ``AsyncSoftReadWriteLock``,
  including cached singleton construction and overflow in the default stale threshold. :pr:`724` - by :user:`lprnmns`.
- Honor acquisition timeouts and ``blocking=False`` during ``SoftReadWriteLock`` state-mutex contention, including
  failed writer cleanup. Cross-host recovery of an abandoned ``.state`` marker remains unsupported. :pr:`726` - by
  :user:`Sohel2309`.
- Preserve exception notes and custom attributes when copying or pickling ``Timeout`` and ``SoftFileLockProtocolError``.
  :pr:`729` - by :user:`jackwalkerlabs`.

.. _release-3-32-5:

*********************
 3.32.5 (2026-08-31)
*********************

.. _release-3-32-5-fixed:

Fixed
=====

- ``SoftFileLease.token`` and ``AsyncSoftFileLease.token`` now read ``None`` after a failed acquisition, so a contender
  turned away by a live holder no longer reports a token for a claim it never published. :pr:`721` - by :user:`lprnmns`.
- Document that ``mode`` has no setter: unlike ``poll_interval``, ``timeout``, ``blocking`` and ``lifetime``, it is
  fixed at construction and ``lock.mode = ...`` raises ``AttributeError``. :pr:`716` - by :user:`Gares95`.

.. _release-3-32-4:

*********************
 3.32.4 (2026-08-23)
*********************

.. _release-3-32-4-fixed:

Fixed
=====

- ``StrictSoftFileLock`` always retries a claim read whose first attempt reports the claim as pending, so a first read
  that itself outlasts the retry grace no longer fails closed on a claim it could have read. :pr:`705` - by
  :user:`gaborbernat`.
- ``WindowsFileLock`` waits out a transient ``STATUS_ACCESS_DENIED`` from ``NtCreateFile`` for up to half a second
  before raising ``PermissionError``, since a peer unlinking the lock file as it releases can answer that for a moment;
  a real denial still fails fast. :pr:`705` - by :user:`gaborbernat`.
- Every lock class now escapes the hostname it publishes, so a host whose ``socket.gethostname()`` carries a space, a
  newline or a byte outside UTF-8 no longer writes a marker it reads back as malformed. Such a host used to lose a held
  ``SoftReadWriteLock`` read slot to a peer and could not take a write slot or a ``StrictSoftFileLock`` at all.
  :pr:`709` - by :user:`dxbjavid`.

.. _release-3-32-3:

*********************
 3.32.3 (2026-08-13)
*********************

.. _release-3-32-3-fixed:

Fixed
=====

- The fork-safety audit hook no longer prints ``Exception ignored in audit hook`` with a ``TypeError`` when an audit
  event fires during interpreter shutdown, after CPython has already cleared the module globals. :pr:`703` - by
  :user:`gaborbernat`.

.. _release-3-32-2:

*********************
 3.32.2 (2026-07-29)
*********************

.. _release-3-32-2-fixed:

Fixed
=====

- A ``SoftReadWriteLock`` or ``SoftFileLease`` acquire whose heartbeat thread fails to start now unlinks its marker and
  hands the claim back, instead of leaving an unrefreshed marker a peer takes while the caller believes it still holds
  the lock. :pr:`691` - by :user:`dxbjavid`.
- Canceling an ``AsyncSoftReadWriteLock`` acquire now releases the claim instead of leaking a marker whose heartbeat
  wedges every contender. :pr:`686` - by :user:`dxbjavid`.

.. _release-3-32-0:

*********************
 3.32.0 (2026-07-21)
*********************

.. _release-3-32-0-added:

Added
=====

- Declare support for Python 3.15 and run the test suite against it and its free-threaded build, both currently in beta.
  :pr:`683` - by :user:`gaborbernat`.

.. _release-3-32-0-fixed:

Fixed
=====

- ``SoftReadWriteLock`` closes the directory handle it opens to scan for readers as soon as a scan stops early, rather
  than holding it until the generator is collected. :pr:`685` - by :user:`gaborbernat`.

.. _release-3-31-2:

*********************
 3.31.2 (2026-07-21)
*********************

.. _release-3-31-2-fixed:

Fixed
=====

- ``filelock`` imports again on runtimes whose ``errno`` omits ``ENOTSUP``, such as GraalPy, where importing the package
  raised ``ImportError``. It probes the code instead, preferring ``ENOTSUP``, falling back to ``EOPNOTSUPP`` where that
  name is absent, and dropping to ``ENOSYS``/``EXDEV`` where neither exists. Platforms defining ``ENOTSUP`` keep their
  behavior. :pr:`681` - by :user:`gaborbernat`.

.. _release-3-31-1:

*********************
 3.31.1 (2026-07-20)
*********************

.. _release-3-31-1-fixed:

Fixed
=====

- A ``SoftFileLease`` acquired on one thread keeps its claim when another thread fails to acquire the same lease object,
  so its heartbeat carries on refreshing the marker instead of being torn down and letting a peer take the live claim.
  :pr:`680` - by :user:`dxbjavid`.

.. _release-3-31-0:

*********************
 3.31.0 (2026-07-18)
*********************

.. _release-3-31-0-added:

Added
=====

- Support Termux/Android, whose CPython ships without ``os.link`` and reports ``sys.platform == "android"``. ``import
  filelock`` and both ``FileLock`` and ``SoftFileLock`` now work there, ``StrictSoftFileLock`` reports its missing
  hard-link support only when acquired, and process liveness reads ``/proc`` on Android instead of PID-only checks.
  :pr:`678` - by :user:`gaborbernat`.

.. _release-3-31-0-fixed:

Fixed
=====

- ``StrictSoftFileLock`` no longer lets two processes hold the lock at once under heavy contention. A holder now keeps
  its intent claim for the whole hold, so a contender whose directory scan races the holder's freshly linked claim can
  no longer miss it and win alongside it. A held lock now exposes both an ``intent`` and a ``held`` claim. :pr:`678` -
  by :user:`gaborbernat`.

.. _release-3-30-3:

*********************
 3.30.3 (2026-07-17)
*********************

.. _release-3-30-3-fixed:

Fixed
=====

- ``AsyncFileLock`` and ``AsyncSoftFileLock`` no longer raise a deadlock ``RuntimeError`` when a different asyncio task
  waits for a lock they hold; the check now scopes holders to the owning task, so only a same-task reacquire is refused.
  :pr:`676` - by :user:`xt-yin`.

.. _release-3-30-3-documentation:

Documentation
=============

- Keep both tables of contents on screen at any browser font size. The widened body column sized itself in ``em``
  against the reader's font size while the breakpoints that fold the layout resolve ``em`` against a fixed 16px, so a
  reader whose default font is larger than 16px had the right-hand table of contents clipped off the edge. The body
  column now flexes instead, and the right-hand table of contents hides only at the mobile breakpoint. :pr:`673` - by
  :user:`gaborbernat`.
- Color the mermaid diagrams from whichever theme is active. They previously carried a light palette baked into each
  diagram's source, which stayed light on a dark page. :pr:`674` - by :user:`gaborbernat`.

.. _release-3-30-2:

*********************
 3.30.2 (2026-07-16)
*********************

.. _release-3-30-2-fixed:

Fixed
=====

- Stop :class:`~filelock.SoftFileLease` deleting a live marker whose ``mode`` it does not implement. An unrecognized
  mode now names its owner instead of reading as malformed, so a record written by a newer filelock survives the grace
  window rather than being evicted after two seconds. :pr:`672` - by :user:`gaborbernat`.
- Stop :class:`~filelock.StrictSoftFileLock` and :class:`~filelock.AsyncStrictSoftFileLock` calling themselves a native
  OS lock when they warn that they ignore ``lifetime``; they now say a strict claim is only ever cleared by
  ``force_break()``. :pr:`672` - by :user:`gaborbernat`.

.. _release-3-30-2-documentation:

Documentation
=============

- Cover every lock type in the tutorials and how-to guides, with examples drawn from projects that use filelock, and
  color the mermaid diagrams. Correct the claims that ``StrictSoftFileLock`` exposes ``owner``, that
  :class:`~filelock.SoftFileLock` evicts a strict sentinel, that :class:`~filelock.ReadWriteLock` requires a ``.db``
  extension, and that every log record is ``DEBUG``. :pr:`672` - by :user:`gaborbernat`.

.. _release-3-30-1:

*********************
 3.30.1 (2026-07-16)
*********************

.. _release-3-30-1-fixed:

Fixed
=====

- ``StrictSoftFileLock`` and ``AsyncStrictSoftFileLock`` no longer abort acquisition when a peer's claim vanishes as an
  NFSv3 stale filehandle instead of a clean removal; the reader revalidates and skips it, so strict locks hold mutual
  exclusion across independent NFSv3 client caches. :pr:`669` - by :user:`gaborbernat`.
- ``SoftFileLease`` reads a marker whose lease duration is ``nan`` or ``inf`` as malformed rather than as a valid lease,
  so such a marker ages out through the grace window instead of raising ``LeaseSettingsMismatch`` on every contender.
  :pr:`670` - by :user:`dxbjavid`.

.. _release-3-30-0:

*********************
 3.30.0 (2026-07-16)
*********************

.. _release-3-30-0-added:

Added
=====

- Add ``context_error_policy`` to surface body and release failures as a :exc:`BaseExceptionGroup`. :pr:`618` - by
  :user:`gaborbernat`.
- Add ``close_error_policy`` to control an :func:`os.close` failure after the OS unlock. :pr:`619` - by
  :user:`gaborbernat`.
- Add :func:`~filelock.lock_descriptor` and :func:`~filelock.unlock_descriptor` to lock a caller-owned file descriptor.
  :pr:`620` - by :user:`gaborbernat`.
- Add ``fallback_to_soft`` to fail closed instead of downgrading to :class:`~filelock.SoftFileLock` on ``ENOSYS``.
  :pr:`622` - by :user:`gaborbernat`.
- Add ``preserve_lock_file`` so native locks keep the lock pathname on release. :pr:`624` - by :user:`gaborbernat`.
- Add an ``on_acquired`` hook that runs a callback on the locked descriptor once the lock is held. :pr:`625` - by
  :user:`gaborbernat`.
- Add ``StrictSoftFileLock``, which treats every marker it did not publish as contention, and ``SoftFileLease``, whose
  claim expires and whose holder learns through ``on_compromise`` when it is lost. ``SoftFileLock(lifetime=...)`` now
  warns and names them. Both publish a record ``SoftFileLock`` evicts, so do not mix contracts on one path. :pr:`658` -
  by :user:`gaborbernat`.
- Add ``StrictSoftFileLock`` and ``AsyncStrictSoftFileLock`` with owner-specific hard-linked claims. Strict mode fails
  closed on claim damage, exposes each recovery token, and requires an exact claim name for a force break. :pr:`659` -
  by :user:`gaborbernat`.

.. _release-3-30-0-changes:

Changes
=======

- Make native lock release transactional so a failed unlock is retried, not dropped. :pr:`615` - by :user:`gaborbernat`.

.. _release-3-30-0-fixed:

Fixed
=====

- Ignore ``lifetime`` on native locks with a warning; only :class:`~filelock.SoftFileLock` honors it. :pr:`593` - by
  :user:`gaborbernat`.
- Stop mutating the Unix lock file before :func:`fcntl.flock` is held. :pr:`594` - by :user:`gaborbernat`.
- Evict a non-regular soft lock file without reading it. :pr:`597` - by :user:`dxbjavid`.
- Write a soft lock's holder record atomically so a short write cannot create overlapping holders. :pr:`614` - by
  :user:`gaborbernat`.
- Open the Windows lock file via ``NtCreateFile`` so a real access denial is raised, not reported as a timeout.
  :pr:`617` - by :user:`gaborbernat`.
- Canonicalize singleton keys so equivalent path spellings share an instance, without following a final symlink.
  :pr:`621` - by :user:`gaborbernat`.
- Preserve parent lock ownership across ``fork()``. A child closes an inherited descriptor only while its identity still
  matches, so it neither unlocks nor unlinks the parent's lock. It clears inherited ownership state and singleton
  caches, then requires a new lock instance before acquiring. Fork-time construction replaces inherited cache mutexes
  and omits an instance whose construction crossed into the child from the cache. If ``fstat()`` cannot establish the
  identity, child cleanup skips the descriptor number because an earlier fork callback may have reused it. Descriptor
  tracking also covers third-party backends that fail after assigning a descriptor. :pr:`652` - by :user:`gaborbernat`.
- Open a SQLite connection per outer acquisition and close it on final release, so a forked child can no longer close
  its parent's handle. A child rejects inherited databases until ``exec()``, and a PyPy child rejects read-write locks
  once the parent has used SQLite. :pr:`657` - by :user:`gaborbernat`.
- Keep executor-backed lock operations alive until they finish when a caller cancels. Serialize acquisitions on the same
  async lock so cancellation rollback cannot release a later caller's hold. Roll back acquisitions that finish after
  cancellation and surface release failures. SQLite read-write locks keep their state until rollback ends the
  transaction; callers can retry cleanup with ``release(force=True)`` or another acquisition. :pr:`652` - by
  :user:`gaborbernat`.
- Reject negative and non-finite ``lifetime`` values during lock construction and assignment. :pr:`644` - by
  :user:`gaborbernat`.
- Read the saved Windows process-probe error before deciding that a soft-lock holder has exited. :pr:`645` - by
  :user:`gaborbernat`.
- Reject non-default lock options that a narrow subclass constructor cannot honor, and forward all options through
  subclasses that accept arbitrary keyword arguments. :pr:`646` - by :user:`gaborbernat`.
- Release now removes the path identity that acquisition recorded instead of resolving a symlinked parent again. When
  acquisition raises, its rollback no longer deletes the holder's deadlock record. :pr:`647` - by :user:`gaborbernat`.
- Remove the body error from implicit context on the release error before adding both errors to an exception group.
  :pr:`648` - by :user:`gaborbernat`.
- Relinquish a soft lock's descriptor before one close attempt so a later release cannot close a reused descriptor
  number, and honor ``close_error_policy`` while still attempting marker cleanup. :pr:`649` - by :user:`gaborbernat`.
- Raise ``OSError(errno.ENOSYS)`` from descriptor locks when ``fcntl`` is unavailable, and reject invalid blocking poll
  intervals before attempting a lock. :pr:`650` - by :user:`gaborbernat`.
- ``SoftFileLock`` and ``SoftFileLease`` record the holder's process start time on every platform and reclaim a stale
  marker only when the recorded owner is provably gone. A reused PID on Unix no longer breaks a live lock or wedges
  acquisition, matching the recycled-PID detection Windows already had. :pr:`660` - by :user:`gaborbernat`.
- ``SoftReadWriteLock`` and ``SoftFileLease`` ride out a transient filesystem error during a heartbeat instead of
  dropping the lock or reporting a false compromise, and report a lost claim only once refresh cannot recover before the
  lease would lapse. ``SoftFileLease`` also reclaims a malformed or partial marker that used to block every contender,
  and a Linux marker distinguishes a PID reused across a reboot. :pr:`661` - by :user:`gaborbernat`.
- ``BaseFileLock.__del__`` suppresses a release error rather than raising it during garbage collection, where it
  surfaced as an unraisable-exception warning attributed to unrelated code. :pr:`665` - by :user:`gaborbernat`.

.. _release-3-30-0-documentation:

Documentation
=============

- Correct the Unix lock-file cleanup and :func:`fcntl.flock` documentation. :pr:`623` - by :user:`gaborbernat`.
- Refresh the documentation to the current code: a new trust-boundaries and ownership-scope section (same-UID boundary,
  advisory native locks, strict claims and lease fencing, a filesystem support matrix, migration off timed stale
  breaking), the lock-selection flowchart and comparison tables extended with ``StrictSoftFileLock`` and
  ``SoftFileLease``, and the cross-platform process start token replacing the Windows-only description throughout.
  :pr:`663` - by :user:`gaborbernat`.
- The filesystem support matrix records mutual exclusion measured across two independent client caches in CI: NFS (v4
  and v3) for the native, soft, and strict locks, and SMB for the native and soft locks. :pr:`665` - by
  :user:`gaborbernat`.

.. _release-3-30-0-security:

Security
========

- Bind the Windows reparse-point check to the locked handle to close a symlink-swap race. :pr:`596` - by
  :user:`gaborbernat`.

.. _release-3-29-7:

*********************
 3.29.7 (2026-07-07)
*********************

.. _release-3-29-7-fixed:

Fixed
=====

- Evict a non-regular write marker without reading it :pr:`588` - by :user:`gaborbernat`.
- Detect cross-instance reentrant deadlocks before the poll loop
  https://github.com/tox-dev/filelock/commit/616400e382cdc9cc8c95823014e4d2d61a70095c - by :user:`HrachShah`

.. _release-3-29-6:

*********************
 3.29.6 (2026-07-06)
*********************

.. _release-3-29-6-fixed:

Fixed
=====

- Serialize singleton construction in ``FileLockMeta`` :pr:`581` - by :user:`dxbjavid`

.. _release-3-29-5:

*********************
 3.29.5 (2026-07-02)
*********************

.. _release-3-29-5-changes:

Changes
=======

- Use a private break name in ``break_lock_file`` :pr:`576` - by :user:`dxbjavid`

.. _release-3-29-5-fixed:

Fixed
=====

- Reject negative, non-numeric, and bool values at the setter
  https://github.com/tox-dev/filelock/commit/be562277636140c26578a98a2a487fdb6f446ee6 - by :user:`HrachShah`
- Roll back a read acquire's open transaction when its SELECT fails :pr:`575` - by :user:`dxbjavid`
- Keep Unix lock files after release :pr:`577` - by :user:`itscloud0`
- Don't complete a writer acquire on a peer's reclaimed marker :pr:`571` - by :user:`dxbjavid`
- Don't follow symlinks in ``raise_on_not_writable_file`` :pr:`567` - by :user:`dxbjavid`
- Only unlink the writer marker on release if it is still ours :pr:`566` - by :user:`dxbjavid`
- Don't follow symlinks when refreshing soft read/write lock markers :pr:`565` - by :user:`dxbjavid`
- Serialize read/write release rollback against a concurrent acquire :pr:`563` - by :user:`dxbjavid`

.. _release-3-29-4:

*********************
 3.29.4 (2026-06-13)
*********************

.. _release-3-29-4-fixed:

Fixed
=====

- Keep the read/write heartbeat alive on a transient touch error :pr:`562` - by :user:`dxbjavid`
- Verify inode in ``break_lock_file`` before unlinking a stale lock :pr:`561` - by :user:`dxbjavid`

.. _release-3-29-3:

*********************
 3.29.3 (2026-06-10)
*********************

.. _release-3-29-3-fixed:

Fixed
=====

- Validate pid range in ``_parse_lock_holder`` :pr:`556` - by :user:`dxbjavid`

.. _release-3-29-2:

*********************
 3.29.2 (2026-06-10)
*********************

.. _release-3-29-2-fixed:

Fixed
=====

- Check hostname in ``is_lock_held_by_us`` :pr:`553` - by :user:`dxbjavid`
- Recover malformed soft-lock markers without breaking a live lock. :pr:`551` - by :user:`gaborbernat`.

.. _release-3-29-2-security:

Security
========

- Open marker reads non-blocking to refuse attacker-placed fifo :pr:`549` - by :user:`dxbjavid`

.. _release-3-29-1:

*********************
 3.29.1 (2026-06-03)
*********************

.. _release-3-29-1-documentation:

Documentation
=============

- Clarify per-thread scope of :class:`~filelock.FileLock` configuration :pr:`543` - by :user:`Gares95`
- Fix API docs of :meth:`~filelock.BaseFileLock.release` :pr:`540` - by :user:`MrAnno`

.. _release-3-29-1-security:

Security
========

- Refuse symlinks when reading a soft lock file. :pr:`548` - by :user:`dxbjavid`.

.. _release-3-29-0:

*********************
 3.29.0 (2026-04-19)
*********************

.. _release-3-29-0-added:

Added
=====

- Detect stale soft locks on Windows. :pr:`534` - by :user:`gaborbernat`.

.. _release-3-29-0-fixed:

Fixed
=====

- Use one executor thread for async lock operations to keep acquisition and release ordered. :pr:`533` - by
  :user:`gaborbernat`.

.. _release-3-28-0:

*********************
 3.28.0 (2026-04-14)
*********************

.. _release-3-28-0-added:

Added
=====

- Inspect a soft lock's owner PID and break stale locks. :pr:`524` - by :user:`gaborbernat`.
- Add :class:`~filelock.SoftReadWriteLock` for NFS and HPC clusters. :pr:`528` - by :user:`gaborbernat`.

.. _release-3-28-0-fixed:

Fixed
=====

- Handle ``__exit__`` and event-loop shutdown for :class:`~filelock.BaseAsyncFileLock`. :pr:`518` - by :user:`naarob`.

.. _release-3-25-2:

*********************
 3.25.2 (2026-03-11)
*********************

.. _release-3-25-2-fixed:

Fixed
=====

- Suppress ``EIO`` when closing a Unix lock on a Docker bind mount. :pr:`513` - by :user:`gaborbernat`.

.. _release-3-25-1:

*********************
 3.25.1 (2026-03-09)
*********************

.. _release-3-25-1-fixed:

Fixed
=====

- Restore best-effort lock file cleanup on Windows. :pr:`511` - by :user:`gaborbernat`.

.. _release-3-25-1-documentation:

Documentation
=============

- Add the project logo to the documentation. :pr:`507` - by :user:`gaborbernat`.

.. _release-3-25-0:

*********************
 3.25.0 (2026-03-01)
*********************

.. _release-3-25-0-added:

Added
=====

- Add :class:`~filelock.AsyncReadWriteLock`. :pr:`506` - by :user:`gaborbernat`.

.. _release-3-25-0-fixed:

Fixed
=====

- Suppress :class:`ValueError` while checking a corrupt stale-lock marker. :pr:`496` - by :user:`bysiber`
- Fix nonblocking transaction-lock acquisition with a timeout. :pr:`498` - by :user:`gaborbernat`

.. _release-3-24-3:

*********************
 3.24.3 (2026-02-19)
*********************

.. _release-3-24-3-fixed:

Fixed
=====

- Handle an ``ENOENT`` race while acquiring a lock on FUSE or NFS. :pr:`495` - by :user:`gaborbernat`.

.. _release-3-24-2:

*********************
 3.24.2 (2026-02-16)
*********************

.. _release-3-24-2-fixed:

Fixed
=====

- Close SQLite cursors and avoid a :class:`~filelock.SoftFileLock` race on Windows. :pr:`491` - by :user:`gaborbernat`.

.. _release-3-24-2-documentation:

Documentation
=============

- Organize the documentation into tutorials, how-to guides, reference, and explanations. :pr:`489` - by
  :user:`gaborbernat`.

.. _release-3-24-1:

*********************
 3.24.1 (2026-02-15)
*********************

.. _release-3-24-1-fixed:

Fixed
=====

- Fix a soft-lock deadlock on Windows. :pr:`488` - by :user:`gaborbernat`.

.. _release-3-24-0:

*********************
 3.24.0 (2026-02-14)
*********************

.. _release-3-24-0-added:

Added
=====

- Add a ``lifetime`` parameter for lock expiration (:issue:`68`). :pr:`486` - by :user:`gaborbernat`.
- Add ``cancel_check`` to :meth:`~filelock.BaseFileLock.acquire` (:issue:`309`). :pr:`487` - by :user:`gaborbernat`.
- Honor POSIX default ACLs when creating a lock file (:issue:`378`). :pr:`483` - by :user:`gaborbernat`.
- Add ``poll_interval`` to the lock constructor. :pr:`482` - by :user:`gaborbernat`.

.. _release-3-24-0-fixed:

Fixed
=====

- Detect a same-thread self-deadlock. :pr:`481` - by :user:`gaborbernat`.
- Fix a lock file race between threads on Windows. :pr:`484` - by :user:`gaborbernat`.
- Fall back to :class:`~filelock.SoftFileLock` when native locking fails with ``ENOSYS``. :pr:`480` - by
  :user:`gaborbernat`.

.. _release-3-23-0:

*********************
 3.23.0 (2026-02-14)
*********************

.. _release-3-23-0-changes:

Changes
=======

- Change the project license from Unlicense to MIT. :pr:`479` - by :user:`gaborbernat`.

.. _release-3-23-0-documentation:

Documentation
=============

- List fasteners among the similar libraries in the documentation. :pr:`478` - by :user:`gaborbernat`.

.. _release-3-22-0:

*********************
 3.22.0 (2026-02-14)
*********************

.. _release-3-22-0-added:

Added
=====

- Detect and break stale soft locks. :pr:`476` - by :user:`gaborbernat`.

.. _release-3-22-0-fixed:

Fixed
=====

- Disable stale-lock detection on Windows until its implementation is safe. :pr:`477` - by :user:`gaborbernat`.

.. _release-3-21-2:

*********************
 3.21.2 (2026-02-13)
*********************

.. _release-3-21-2-fixed:

Fixed
=====

- Handle :exc:`ImportError` when the :mod:`sqlite3` C library is unavailable. :pr:`475` - by :user:`gaborbernat`.

.. _release-3-21-1:

*********************
 3.21.1 (2026-02-12)
*********************

.. _release-3-21-1-fixed:

Fixed
=====

- Import :class:`~filelock.ReadWriteLock` when :mod:`sqlite3` is unavailable. :pr:`473` - by :user:`bayandin`.

.. _release-3-21-0:

*********************
 3.21.0 (2026-02-12)
*********************

.. _release-3-20-4:

**********************
 v3.20.4 (2026-03-23)
**********************

.. _release-3-20-4-added:

Added
=====

- Add a SQLite-backed :class:`~filelock.ReadWriteLock`. :pr:`399` - by :user:`leventov`
- Allow Unix lock files to be removed after release. :pr:`408` - by :user:`sbc100`

.. _release-3-20-3:

**********************
 v3.20.3 (2026-01-09)
**********************

.. _release-3-20-3-security:

Security
========

- Fix TOCTOU symlink vulnerability in :class:`~filelock.SoftFileLock` :pr:`465` - by :user:`gaborbernat`.

.. _release-3-20-2:

**********************
 v3.20.2 (2026-01-02)
**********************

.. _release-3-20-2-added:

Added
=====

- Support Unix systems without :data:`os.O_NOFOLLOW` :pr:`463` - by :user:`mwilliamson`.

.. _release-3-20-1:

**********************
 v3.20.1 (2025-12-15)
**********************

.. _release-3-20-1-security:

Security
========

- Fix TOCTOU symlink vulnerability in lock file creation :pr:`461` - by :user:`gaborbernat`.

.. _release-3-20-0:

**********************
 v3.20.0 (2025-10-08)
**********************

.. _release-3-20-0-added:

Added
=====

- Add Python 3.14 support, drop 3.9 :pr:`448` - by :user:`gaborbernat`.

.. _release-3-19-1:

**********************
 v3.19.1 (2025-08-14)
**********************

.. _release-3-19-1-added:

Added
=====

- Add support for Python 3.14 :pr:`432` - by :user:`gaborbernat`.

.. _release-3-18-0:

**********************
 v3.18.0 (2025-03-11)
**********************

.. _release-3-18-0-added:

Added
=====

- Support :mod:`fcntl` check on Emscripten :pr:`398` - by :user:`juntyr`.

.. _release-3-18-0-fixed:

Fixed
=====

- Indicate that locks are exclusive/write locks :pr:`394` - by :user:`bicarlsen`.

.. _release-3-17-0:

**********************
 v3.17.0 (2025-01-21)
**********************

.. _release-3-17-0-added:

Added
=====

- Drop Python 3.8 support :pr:`388`.

.. _release-3-16-1:

**********************
 v3.16.1 (2024-09-17)
**********************

.. _release-3-16-0:

**********************
 v3.16.0 (2024-09-07)
**********************

.. _release-3-16-0-added:

Added
=====

- Add Python 3.13 to CI :pr:`359` - by :user:`gaborbernat`.

.. _release-3-15-4:

**********************
 v3.15.4 (2024-06-22)
**********************

.. _release-3-15-4-fixed:

Fixed
=====

- Pass ``file_lock`` as positional argument :pr:`347` - by :user:`kwist-sgr`.

.. _release-3-15-3:

**********************
 v3.15.3 (2024-06-19)
**********************

.. _release-3-15-3-fixed:

Fixed
=====

- Fix ``TypeError: _CountedFileLock.__init__() got an unexpected keyword argument`` :pr:`345` - by :user:`kwist-sgr`.

.. _release-3-15-2:

**********************
 v3.15.2 (2024-06-19)
**********************

.. _release-3-15-2-changes:

Changes
=======

- Use a metaclass to implement the singleton pattern :pr:`340` - by :user:`kwist-sgr`.

.. _release-3-15-1:

**********************
 v3.15.1 (2024-06-12)
**********************

.. _release-3-15-1-fixed:

Fixed
=====

- Restore ``__init__`` method; more robust initialization for singleton locks :pr:`338` - by :user:`ethanbb`.

.. _release-3-15-0:

**********************
 v3.15.0 (2024-06-11)
**********************

.. _release-3-15-0-added:

Added
=====

- Add asyncio support :pr:`332` - by :user:`Ovizro`.

.. _release-3-15-0-fixed:

Fixed
=====

- Don't re-initialize :class:`~filelock.BaseFileLock` when returning existing singleton instance :pr:`334` - by
  :user:`ethanbb`.

.. _release-3-14-0:

**********************
 v3.14.0 (2024-04-27)
**********************

.. _release-3-14-0-added:

Added
=====

- Add ``blocking`` parameter on lock constructor :pr:`325` - by :user:`iamkhav`.

.. _release-3-13-4:

**********************
 v3.13.4 (2024-04-09)
**********************

.. _release-3-13-4-fixed:

Fixed
=====

- Raise error on incompatible singleton ``timeout`` and ``mode`` arguments :pr:`320` - by :user:`nefrob`.

.. _release-3-13-3:

**********************
 v3.13.3 (2024-03-25)
**********************

.. _release-3-13-3-changes:

Changes
=======

- Make singleton class instance dict unique per subclass :pr:`318` - by :user:`nefrob`.

.. _release-3-13-2:

**********************
 v3.13.2 (2024-03-25)
**********************

.. _release-3-13-2-fixed:

Fixed
=====

- Fix permission denied error when lock file is placed in ``/tmp`` :pr:`317` - by :user:`kota-iizuka`.

.. _release-3-13-1:

**********************
 v3.13.1 (2023-10-30)
**********************

.. _release-3-13-1-added:

Added
=====

- Allow users to subclass :class:`~filelock.FileLock` with custom keyword arguments :pr:`284` - by :user:`hmaarrfk`.

.. _release-3-13-0:

**********************
 v3.13.0 (2023-10-27)
**********************

.. _release-3-13-0-added:

Added
=====

- Support reentrant locking on lock file path via optional ``is_singleton`` instance :pr:`283` - by :user:`nefrob`.

.. _release-3-12-4:

**********************
 v3.12.4 (2023-09-13)
**********************

.. _release-3-12-4-packaging:

Packaging
=========

- Change ``typing-extensions`` to be installed only with the ``[typing]`` extra :pr:`276` - by :user:`asottile`.

.. _release-3-12-3:

**********************
 v3.12.3 (2023-08-28)
**********************

.. _release-3-12-3-added:

Added
=====

- Create parent directories if necessary :pr:`254` - by :user:`gerlero`.

.. _release-3-12-2:

**********************
 v3.12.2 (2023-06-12)
**********************

.. _release-3-12-2-fixed:

Fixed
=====

- Restore ``if TYPE_CHECKING`` syntax for :class:`~filelock.FileLock` definition :pr:`245` - by :user:`dlax`.

.. _release-3-12-1:

**********************
 v3.12.1 (2023-06-10)
**********************

.. _release-3-12-1-added:

Added
=====

- Add Python 3.12 support :pr:`237` - by :user:`gaborbernat`.

.. _release-3-12-0:

**********************
 v3.12.0 (2023-04-18)
**********************

.. _release-3-12-0-changes:

Changes
=======

- Allow callers to disable thread-local lock state at construction; it remains enabled by default. :pr:`232` - by
  :user:`csm10495`.

.. _release-3-12-0-fixed:

Fixed
=====

- Fix a lock hang on Windows. :pr:`231` - by :user:`TheMatt2`.

.. _release-3-11-0:

**********************
 v3.11.0 (2023-04-06)
**********************

.. _release-3-11-0-changes:

Changes
=======

- Keep lock state local to each thread. :pr:`219` - by :user:`csm10495`.

.. _release-3-10-7:

**********************
 v3.10.7 (2023-03-27)
**********************

.. _release-3-10-7-fixed:

Fixed
=====

- Use :func:`os.fchmod` to avoid a :func:`os.chmod` failure on PyPy via Anaconda. :pr:`214` - by :user:`jfennick`.

.. _release-3-10-6:

**********************
 v3.10.6 (2023-03-25)
**********************

.. _release-3-10-6-fixed:

Fixed
=====

- Raise unexpected file-opening errors from :class:`~filelock.SoftFileLock` instead of repeatedly waiting for a lock
  that cannot be acquired. :pr:`213` - by :user:`jahrules` and :user:`gaborbernat`.

.. _release-3-10-5:

**********************
 v3.10.5 (2023-03-25)
**********************

.. _release-3-10-5-added:

Added
=====

- Raise :class:`NotImplementedError` when a Unix filesystem does not support ``flock`` instead of waiting indefinitely.
  :pr:`212` - by :user:`jahrules`.

.. _release-3-10-4:

**********************
 v3.10.4 (2023-03-24)
**********************

.. _release-3-10-4-fixed:

Fixed
=====

- Apply the requested file mode when creating a Unix lock file, including when the file cannot later be changed with
  ``chmod``. :pr:`211` - by :user:`jahrules`.

.. _release-3-10-3:

**********************
 v3.10.3 (2023-03-23)
**********************

.. _release-3-10-3-fixed:

Fixed
=====

- Fix permission issue - by :user:`jahrules`.

.. _release-3-10-2:

**********************
 v3.10.2 (2023-03-22)
**********************

.. _release-3-10-2-fixed:

Fixed
=====

- Bug fix for using filelock with threaded programs causing undesired file permissions - by :user:`jahrules`.

.. _release-3-10-1:

**********************
 v3.10.1 (2023-03-22)
**********************

.. _release-3-10-1-fixed:

Fixed
=====

- Handle pickle for :class:`~filelock.Timeout` :pr:`203` - by :user:`TheMatt2`.

.. _release-3-10-0:

**********************
 v3.10.0 (2023-03-15)
**********************

.. _release-3-10-0-added:

Added
=====

- Add support for explicit file modes for lockfiles :pr:`192` - by :user:`jahrules`.

.. _release-3-9-1:

*********************
 v3.9.1 (2023-03-14)
*********************

.. _release-3-9-1-changes:

Changes
=======

- Use :func:`time.perf_counter` instead of :func:`time.monotonic` to calculate timeouts. :pr:`194` - by :user:`zpz`.

.. _release-3-9-0:

*********************
 v3.9.0 (2022-12-28)
*********************

.. _release-3-8-2:

*********************
 v3.8.2 (2022-12-05)
*********************

.. _release-3-8-2-packaging:

Packaging
=========

- Update the published license classifier to Unlicense. :pr:`180` - by :user:`jond01`

.. _release-3-8-1:

*********************
 v3.8.1 (2022-12-04)
*********************

.. _release-3-8-1-fixed:

Fixed
=====

- Make :class:`~filelock.FileLock` valid in mypy type annotations. :pr:`177` - by :user:`gaborbernat`.

.. _release-3-8-0:

*********************
 v3.8.0 (2022-12-04)
*********************

.. _release-3-8-0-added:

Added
=====

- Add Python 3.11 support. :pr:`158` - by :user:`gaborbernat`.

.. _release-3-8-0-documentation:

Documentation
=============

- State timeout units in the API docstrings. :pr:`148` - by :user:`jnordberg`.

.. _release-3-7-1:

*********************
 v3.7.1 (2022-05-31)
*********************

.. _release-3-7-1-documentation:

Documentation
=============

- Point README documentation links to the site index - by :user:`gaborbernat`.

.. _release-3-7-0:

*********************
 v3.7.0 (2022-05-13)
*********************

.. _release-3-7-0-added:

Added
=====

- Allow a lock attempt to return immediately when the lock cannot be acquired. :pr:`142` - by :user:`myheroyuki`.

.. _release-3-6-0:

*********************
 v3.6.0 (2022-02-17)
*********************

.. _release-3-6-0-fixed:

Fixed
=====

- Fix pylint warning "Abstract class :class:`~filelock.WindowsFileLock` with abstract methods instantiated" :pr:`135` -
  by :user:`vonschultz`
- Fix pylint warning "Abstract class :class:`~filelock.UnixFileLock` with abstract methods instantiated" :pr:`135` - by
  :user:`vonschultz`

.. _release-3-5-1:

*********************
 v3.5.1 (2022-02-16)
*********************

.. _release-3-5-1-fixed:

Fixed
=====

- Use a monotonic clock for timeouts so wall-clock changes do not change their duration. :pr:`136` - by :user:`avian2`.

.. _release-3-5-0:

*********************
 v3.5.0 (2022-02-15)
*********************

.. _release-3-5-0-added:

Added
=====

- Allow a lock to be used as a context decorator. :pr:`128` - by Hirsch Alter.

.. _release-3-4-2:

*********************
 v3.4.2 (2021-12-16)
*********************

.. _release-3-4-2-added:

Added
=====

- Drop Python 3.6 support. :pr:`125` - by :user:`gaborbernat`.

.. _release-3-4-1:

*********************
 v3.4.1 (2021-12-16)
*********************

.. _release-3-4-1-fixed:

Fixed
=====

- Point argument deprecation warnings at the caller by setting ``stacklevel``. :pr:`121` - by :user:`XuehaiPan`.

.. _release-3-4-0:

*********************
 v3.4.0 (2021-11-16)
*********************

.. _release-3-4-0-deprecated:

Deprecated
==========

- Add correct spelling of poll interval parameter for :meth:`~filelock.BaseFileLock.acquire` method, raise deprecation
  warning when using the misspelled form :pr:`119` - by :user:`XuehaiPan`.

.. _release-3-3-2:

*********************
 v3.3.2 (2021-10-29)
*********************

.. _release-3-3-2-added:

Added
=====

- Accept :class:`pathlib.Path` and :class:`pathlib.PurePath` in the :class:`~filelock.FileLock` constructor. :pr:`110` -
  by :user:`f-koehler`.

.. _release-3-3-1:

*********************
 v3.3.1 (2021-10-15)
*********************

.. _release-3-3-1-fixed:

Fixed
=====

- Leave the log level of the ``filelock`` logger as not set (previously was set to warning) :pr:`108` - by
  :user:`gaborbernat`

.. _release-3-3-1-documentation:

Documentation
=============

- Add changelog to the documentation :pr:`108` - by :user:`gaborbernat`

.. _release-3-3-0:

*********************
 v3.3.0 (2021-10-03)
*********************

.. _release-3-3-0-added:

Added
=====

- Drop Python 2.7 and 3.5 support. :pr:`100` - by :user:`gaborbernat`.
- Add type annotations. :pr:`100` - by :user:`gaborbernat`.

.. _release-3-3-0-documentation:

Documentation
=============

- Document asyncio support - by :user:`gaborbernat`

.. _release-3-2-1:

*********************
 v3.2.1 (2021-10-02)
*********************

.. _release-3-2-1-fixed:

Fixed
=====

- Changed logger name from ``filelock._api`` to ``filelock`` :pr:`97` - by :user:`hkennyv`

.. _release-3-2-1-documentation:

Documentation
=============

- Add a tutorial covering lock acquisition, timeouts, and recursive use - by :user:`gaborbernat`.

.. _release-3-2-0:

*********************
 v3.2.0 (2021-09-30)
*********************

.. _release-3-2-0-changes:

Changes
=======

- Move lock acquire/release log from INFO to DEBUG :pr:`95` - by :user:`gaborbernat`

.. _release-3-2-0-fixed:

Fixed
=====

- Raise when trying to acquire in R/O or missing folder :pr:`96` - by :user:`gaborbernat`

.. _release-3-1-0:

*********************
 v3.1.0 (2021-09-27)
*********************

.. _release-3-1-0-documentation:

Documentation
=============

- Fix the LICENSE link. :pr:`63` - by :user:`sharkwouter`.
- Update links for the project's move to tox-dev. :pr:`88` - by :user:`hugovk`.

.. _release-3-0-12:

**********************
 v3.0.12 (2019-05-18)
**********************

.. _release-3-0-12-fixed:

Fixed
=====

- Fix package setup on Python 2 - by Benedikt Schmitt.

.. _release-3-0-12-documentation:

Documentation
=============

- Correct the timeout documentation: zero means no wait, while a negative value waits indefinitely. :issue:`49` - by
  Benedikt Schmitt; reported by :user:`AnkurTank`.

.. _release-3-0-12-packaging:

Packaging
=========

- Fix package upload to PyPI by keeping the license summary on one line - by Benedikt Schmitt.
- Update the published PyPI classifiers - by :user:`hugovk`.

.. _release-3-0-10:

**********************
 v3.0.10 (2018-11-01)
**********************

.. _release-3-0-10-documentation:

Documentation
=============

- Fix README rendering on PyPI - by :user:`adamchainz`.

.. _release-3-0-9:

*********************
 v3.0.9 (2018-10-02)
*********************

.. _release-3-0-8:

*********************
 v3.0.8 (2018-09-09)
*********************

.. _release-3-0-8-fixed:

Fixed
=====

- Fix a memory leak when acquiring locks on Python 2. :issue:`37` - by Benedikt Schmitt.

.. _release-3-0-6:

*********************
 v3.0.6 (2018-08-22)
*********************

.. _release-3-0-4:

*********************
 v3.0.4 (2018-02-01)
*********************

.. _release-3-0-3:

*********************
 v3.0.3 (2018-01-30)
*********************

.. _release-3-0-3-documentation:

Documentation
=============

- Render the animated README example through a raw image URL on PyPI - by Benedikt Schmitt.

.. _release-3-0-2:

*********************
 v3.0.2 (2018-01-30)
*********************

.. _release-3-0-2-fixed:

Fixed
=====

- Keep the Unix lock file after release to avoid a race between processes. :issue:`31` - by Benedikt Schmitt.

.. _release-3-0-2-documentation:

Documentation
=============

- Add navigation to the README - by Benedikt Schmitt.
- Clarify when to use a separate lock file in the documentation. :issue:`22` - by Benedikt Schmitt.

.. _release-3-0-0:

*********************
 v3.0.0 (2018-01-05)
*********************

.. _release-3-0-0-changes:

Changes
=======

- Remove the Unix lock file after :meth:`~filelock.BaseFileLock.release` - by :user:`spokorski`.

.. _release-3-0-0-fixed:

Fixed
=====

- Reraise lock errors correctly on Python 3 - by Benedikt Schmitt.

.. _release-2-0-13:

**********************
 v2.0.13 (2017-11-05)
**********************

.. _release-2-0-13-fixed:

Fixed
=====

- Acquire the logger only when first needed. :issue:`24` - by Benedikt Schmitt.

.. _release-2-0-12:

**********************
 v2.0.12 (2017-09-02)
**********************

.. _release-2-0-11:

**********************
 v2.0.11 (2017-07-19)
**********************

.. _release-2-0-11-added:

Added
=====

- Add official Python 2 support. :issue:`20` - by Benedikt Schmitt.

.. _release-2-0-10:

**********************
 v2.0.10 (2017-06-07)
**********************

.. _release-2-0-9:

*********************
 v2.0.9 (2017-06-07)
*********************

.. _release-2-0-9-fixed:

Fixed
=====

- Add example :pr:`16` - by :user:`pypingou`.
- Update change order of the examples (:pr:`16`) - by :user:`pypingou`.

.. _release-2-0-9-documentation:

Documentation
=============

- Explain why the protected file needs a separate lock path. :issue:`19` - by Benedikt Schmitt.
- Correct the Read the Docs URL - by Benedikt Schmitt.

.. _release-2-0-8:

*********************
 v2.0.8 (2017-01-24)
*********************

.. _release-2-0-8-added:

Added
=====

- Add logging for lock operations - by :user:`expobrain`.

.. _release-2-0-7:

*********************
 v2.0.7 (2016-11-05)
*********************

.. _release-2-0-7-packaging:

Packaging
=========

- Keep the README and license out of the installed environment root while including them in the source archive.
  :issue:`14` - by Benedikt Schmitt.

.. _release-2-0-6:

*********************
 v2.0.6 (2016-05-01)
*********************

.. _release-2-0-6-fixed:

Fixed
=====

- Clear the lock descriptor before unlocking on Unix and Windows, avoiding a repeated release. - by :user:`cottsay`.

.. _release-2-0-5:

*********************
 v2.0.5 (2015-11-11)
*********************

.. _release-2-0-5-fixed:

Fixed
=====

- Keep the lock file after release - by :user:`kramarz`.

.. _release-2-0-5-added:

Added
=====

- Export :class:`~filelock.BaseFileLock` as a public class - by Benedikt Schmitt.

.. _release-2-0-5-documentation:

Documentation
=============

- Show the complete public API in the reference page - by Benedikt Schmitt.

.. _release-2-0-4:

*********************
 v2.0.4 (2015-07-29)
*********************

.. _release-2-0-4-added:

Added
=====

- Export the platform-specific lock classes consistently through ``__all__`` - by Benedikt Schmitt.

.. _release-2-0-1:

*********************
 v2.0.1 (2015-06-13)
*********************

.. _release-2-0-1-fixed:

Fixed
=====

- Treat Windows lock contention as a timeout instead of raising ``IOError``. :issue:`5` - by Benedikt Schmitt.
- Avoid an extra acquisition for ``with lock.acquire(timeout=...)``, so one release frees the lock. :issue:`3` - by
  Benedikt Schmitt.

.. _release-2-0-1-documentation:

Documentation
=============

- Add a documentation site with recursive locking examples and an API reference - by Benedikt Schmitt.

.. _release-2-0-0:

*********************
 v2.0.0 (2015-05-25)
*********************

.. _release-2-0-0-added:

Added
=====

- Allow a default timeout at lock construction. :issue:`2` - by Benedikt Schmitt.

.. _release-1-0-3:

*********************
 v1.0.3 (2015-04-22)
*********************

.. _release-1-0-3-fixed:

Fixed
=====

- Handle an exception while acquiring a lock - by Benedikt Schmitt.

.. _release-1-0-2:

*********************
 v1.0.2 (2015-04-22)
*********************

.. _release-1-0-2-fixed:

Fixed
=====

- Do not time out when the same lock object already holds the lock - by Benedikt Schmitt.

.. _release-1-0-1:

*********************
 v1.0.1 (2015-04-22)
*********************

.. _release-1-0-1-fixed:

Fixed
=====

- Return immediately after a successful first acquire instead of waiting one poll interval. :issue:`1` - by Benedikt
  Schmitt.

.. _release-1-0-0:

*********************
 v1.0.0 (2015-04-07)
*********************

.. _release-1-0-0-added:

Added
=====

- Add a lock counter so the same lock object can acquire recursively - by Benedikt Schmitt.

.. _release-0-2-2:

*********************
 v0.2.2 (2015-03-25)
*********************

.. _release-0-2-2-packaging:

Packaging
=========

- Include the renamed README and license files in the source distribution - by Benedikt Schmitt.

.. _release-0-2-1:

*********************
 v0.2.1 (2015-03-25)
*********************

.. _release-0-2-1-packaging:

Packaging
=========

- Publish the README and license as reStructuredText so package metadata can render them - by Benedikt Schmitt.

.. _release-0-2-0:

*********************
 v0.2.0 (2014-07-06)
*********************

.. _release-0-2-0-added:

Added
=====

- Add file locks for Unix and Windows - by Benedikt Schmitt.
