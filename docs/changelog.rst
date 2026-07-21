###########
 Changelog
###########

.. towncrier-draft-entries:: Unreleased

.. towncrier release notes start

********************
 3.32.0 (2026-07-21)
********************

- ``SoftReadWriteLock`` closes the directory handle it opens to scan for readers as soon as a scan stops early, rather
  than holding it until the generator is collected. :pr:`685`
- Declare support for Python 3.15 and run the test suite against it and its free-threaded build, both currently in beta. :pr:`683`
- The source distribution ships the capability probes the tests import, and reading one no longer needs ``coverage``
  installed, so the suite runs from an unpacked sdist instead of failing on a missing ``coverage_pragmas``. :pr:`685`

********************
 3.31.2 (2026-07-21)
********************

- ``filelock`` imports again on runtimes whose ``errno`` omits ``ENOTSUP``, such as GraalPy, where importing the package
  raised ``ImportError``. It probes the code instead, preferring ``ENOTSUP``, falling back to ``EOPNOTSUPP`` where that
  name is absent, and dropping to ``ENOSYS``/``EXDEV`` where neither exists. Platforms defining ``ENOTSUP`` keep their
  behavior. :pr:`681`

********************
 3.31.1 (2026-07-20)
********************

- A ``SoftFileLease`` acquired on one thread keeps its claim when another thread fails to acquire the same lease object,
  so its heartbeat carries on refreshing the marker instead of being torn down and letting a peer take the live claim. :pr:`680`

********************
 3.31.0 (2026-07-18)
********************

- Support Termux/Android, whose CPython ships without ``os.link`` and reports ``sys.platform == "android"``. ``import
  filelock`` and both ``FileLock`` and ``SoftFileLock`` now work there, ``StrictSoftFileLock`` reports its missing
  hard-link support only when acquired, and process liveness reads ``/proc`` on Android instead of PID-only checks. :pr:`678`
- ``StrictSoftFileLock`` no longer lets two processes hold the lock at once under heavy contention. A holder now keeps
  its intent claim for the whole hold, so a contender whose directory scan races the holder's freshly linked claim can no
  longer miss it and win alongside it. A held lock now exposes both an ``intent`` and a ``held`` claim. :pr:`678`

********************
 3.30.3 (2026-07-17)
********************

- ``AsyncFileLock`` and ``AsyncSoftFileLock`` no longer raise a deadlock ``RuntimeError`` when a different asyncio task
  waits for a lock they hold; the check now scopes holders to the owning task, so only a same-task reacquire is refused. :pr:`676`
- Keep both tables of contents on screen at any browser font size. The widened body column sized itself in ``em`` against the reader's font size while the breakpoints that fold the layout resolve ``em`` against a fixed 16px, so a reader whose default font is larger than 16px had the right-hand table of contents clipped off the edge. The body column now flexes instead, and the right-hand table of contents hides only at the mobile breakpoint. :pr:`673`
- Color the mermaid diagrams from whichever theme is active. They previously carried a light palette baked into each
  diagram's source, which stayed light on a dark page. :pr:`674`

********************
 3.30.2 (2026-07-16)
********************

- Stop :class:`~filelock.SoftFileLease` deleting a live marker whose ``mode`` it does not implement. An unrecognized mode now names its owner instead of reading as malformed, so a record written by a newer filelock survives the grace window rather than being evicted after two seconds. :pr:`672`
- Stop :class:`~filelock.StrictSoftFileLock` and :class:`~filelock.AsyncStrictSoftFileLock` calling themselves a native OS lock when they warn that they ignore ``lifetime``; they now say a strict claim is only ever cleared by ``force_break()``. :pr:`672`
- Cover every lock type in the tutorials and how-to guides, with examples drawn from projects that use filelock, and color the mermaid diagrams.
  Correct the claims that ``StrictSoftFileLock`` exposes ``owner``, that :class:`~filelock.SoftFileLock` evicts a strict sentinel, that :class:`~filelock.ReadWriteLock` requires a ``.db`` extension, and that every log record is ``DEBUG``. :pr:`672`

********************
 3.30.1 (2026-07-16)
********************

- ``StrictSoftFileLock`` and ``AsyncStrictSoftFileLock`` no longer abort acquisition when a peer's claim vanishes as an
  NFSv3 stale filehandle instead of a clean removal; the reader revalidates and skips it, so strict locks hold mutual
  exclusion across independent NFSv3 client caches. :pr:`669`
- ``SoftFileLease`` reads a marker whose lease duration is ``nan`` or ``inf`` as malformed rather than as a valid lease,
  so such a marker ages out through the grace window instead of raising ``LeaseSettingsMismatch`` on every contender. :pr:`670`

********************
 3.30.0 (2026-07-16)
********************

- Add ``context_error_policy`` to surface body and release failures as a :exc:`BaseExceptionGroup`. :pr:`618`
- Add ``close_error_policy`` to control an :func:`os.close` failure after the OS unlock. :pr:`619`
- Add :func:`~filelock.lock_descriptor` and :func:`~filelock.unlock_descriptor` to lock a caller-owned file descriptor. :pr:`620`
- Add ``fallback_to_soft`` to fail closed instead of downgrading to :class:`~filelock.SoftFileLock` on ``ENOSYS``. :pr:`622`
- Add ``preserve_lock_file`` so native locks keep the lock pathname on release. :pr:`624`
- Add an ``on_acquired`` hook that runs a callback on the locked descriptor once the lock is held. :pr:`625`
- Add ``StrictSoftFileLock``, which treats every marker it did not publish as contention, and ``SoftFileLease``, whose
  claim expires and whose holder learns through ``on_compromise`` when it is lost. ``SoftFileLock(lifetime=...)`` now warns
  and names them. Both publish a record ``SoftFileLock`` evicts, so do not mix contracts on one path. :pr:`636`
- Add ``StrictSoftFileLock`` and ``AsyncStrictSoftFileLock`` with owner-specific hard-linked claims. Strict mode fails
  closed on claim damage, exposes each recovery token, and requires an exact claim name for a force break. :pr:`637`
- Ignore ``lifetime`` on native locks with a warning; only :class:`~filelock.SoftFileLock` honors it. :pr:`593`
- Stop mutating the Unix lock file before :func:`fcntl.flock` is held. :pr:`594`
- Bind the Windows reparse-point check to the locked handle to close a symlink-swap race. :pr:`596`
- Evict a non-regular soft lock file without reading it. :pr:`597`
- Write a soft lock's holder record atomically so a short write cannot create overlapping holders. :pr:`614`
- Make native lock release transactional so a failed unlock is retried, not dropped. :pr:`615`
- Open the Windows lock file via ``NtCreateFile`` so a real access denial is raised, not reported as a timeout. :pr:`617`
- Canonicalize singleton keys so equivalent path spellings share an instance, without following a final symlink. :pr:`621`
- Preserve parent lock ownership across ``fork()``. A child closes an inherited descriptor only while its identity still
  matches, so it neither unlocks nor unlinks the parent's lock. It clears inherited ownership state and singleton caches,
  then requires a new lock instance before acquiring. Fork-time construction replaces inherited cache mutexes and omits
  an instance whose construction crossed into the child from the cache. If ``fstat()`` cannot establish the identity,
  child cleanup skips the descriptor number because an earlier fork callback may have reused it. Descriptor tracking
  also covers third-party backends that fail after assigning a descriptor. :pr:`634`
- Open a SQLite connection per outer acquisition and close it on final release, so a forked child can no longer close its
  parent's handle. A child rejects inherited databases until ``exec()``, and a PyPy child rejects read-write locks once the
  parent has used SQLite. :pr:`635`
- Keep executor-backed lock operations alive until they finish when a caller cancels. Serialize acquisitions on the same
  async lock so cancellation rollback cannot release a later caller's hold. Roll back acquisitions that finish after
  cancellation and surface release failures. SQLite read-write locks keep their state until rollback ends the transaction;
  callers can retry cleanup with ``release(force=True)`` or another acquisition. :pr:`640`
- Reject negative and non-finite ``lifetime`` values during lock construction and assignment. :pr:`644`
- Read the saved Windows process-probe error before deciding that a soft-lock holder has exited. :pr:`645`
- Reject non-default lock options that a narrow subclass constructor cannot honor, and forward all options through
  subclasses that accept arbitrary keyword arguments. :pr:`646`
- Release now removes the path identity that acquisition recorded instead of resolving a symlinked parent again. When
  acquisition raises, its rollback no longer deletes the holder's deadlock record. :pr:`647`
- Remove the body error from implicit context on the release error before adding both errors to an exception group. :pr:`648`
- Relinquish a soft lock's descriptor before one close attempt so a later release cannot close a reused descriptor number,
  and honor ``close_error_policy`` while still attempting marker cleanup. :pr:`649`
- Raise ``OSError(errno.ENOSYS)`` from descriptor locks when ``fcntl`` is unavailable, and reject invalid blocking poll
  intervals before attempting a lock. :pr:`650`
- ``SoftFileLock`` and ``SoftFileLease`` record the holder's process start time on every platform and reclaim a stale marker only when the recorded owner is provably gone.
  A reused PID on Unix no longer breaks a live lock or wedges acquisition, matching the recycled-PID detection Windows already had. :pr:`660`
- ``SoftReadWriteLock`` and ``SoftFileLease`` ride out a transient filesystem error during a heartbeat instead of dropping the lock or reporting a false compromise, and report a lost claim only once refresh cannot recover before the lease would lapse.
  ``SoftFileLease`` also reclaims a malformed or partial marker that used to block every contender, and a Linux marker distinguishes a PID reused across a reboot. :pr:`661`
- ``BaseFileLock.__del__`` suppresses a release error rather than raising it during garbage collection, where it surfaced as an unraisable-exception warning attributed to unrelated code. :pr:`665`
- Correct the Unix lock-file cleanup and :func:`fcntl.flock` documentation. :pr:`623`
- Drop automated bot entries from the changelog and link its code references to the API docs and the Python standard library. :pr:`638`
- Refresh the documentation to the current code: a new trust-boundaries and ownership-scope section (same-UID boundary, advisory native locks, strict claims and lease fencing, a filesystem support matrix, migration off timed stale breaking), the lock-selection flowchart and comparison tables extended with ``StrictSoftFileLock`` and ``SoftFileLease``, and the cross-platform process start token replacing the Windows-only description throughout. :pr:`663`
- The filesystem support matrix records mutual exclusion measured across two independent client caches in CI: NFS (v4 and v3) for the native, soft, and strict locks, and SMB for the native and soft locks. :pr:`665`
- Build the changelog from towncrier news fragments, render pending fragments as an ``Unreleased`` docs section, and refuse to publish a tag the changelog does not document. :pr:`626`

********************
 3.29.7 (2026-07-07)
********************

- _util: drop the dead ``st_mtime=0`` short-circuit in ``raise_on_not_writable_file`` :pr:`589` - by :user:`HrachShah`
- soft_rw: evict a non-regular write marker without reading it :pr:`588`
- asyncio: detect cross-instance reentrant deadlocks before the poll loop :pr:`586` - by :user:`HrachShah`

********************
 3.29.6 (2026-07-06)
********************

- test: silence fork DeprecationWarning on 3.15 :pr:`585`
- _util: drop the dead ``st_mtime=0`` short-circuit in ``raise_on_not_writable_file`` :pr:`582` - by :user:`HrachShah`
- serialize singleton construction in ``FileLockMeta`` :pr:`581` - by :user:`dxbjavid`
- surface GitHub Sponsors and thanks.dev

********************
 3.29.5 (2026-07-02)
********************

- lifetime: reject negative, non-numeric, and bool values at the setter :pr:`573` - by :user:`HrachShah`
- roll back a read acquire's open transaction when its SELECT fails :pr:`575` - by :user:`dxbjavid`
- keep Unix lock files after release :pr:`577` - by :user:`itscloud0`
- use a private break name in ``break_lock_file`` :pr:`576` - by :user:`dxbjavid`
- don't complete a writer acquire on a peer's reclaimed marker :pr:`571` - by :user:`dxbjavid`
- don't follow symlinks in ``raise_on_not_writable_file`` :pr:`567` - by :user:`dxbjavid`
- only unlink the writer marker on release if it is still ours :pr:`566` - by :user:`dxbjavid`
- don't follow symlinks when refreshing soft read/write lock markers :pr:`565` - by :user:`dxbjavid`
- serialize read/write release rollback against a concurrent acquire :pr:`563` - by :user:`dxbjavid`

********************
 3.29.4 (2026-06-13)
********************

- keep the read/write heartbeat alive on a transient touch error :pr:`562` - by :user:`dxbjavid`
- verify inode in ``break_lock_file`` before unlinking a stale lock :pr:`561` - by :user:`dxbjavid`

********************
 3.29.3 (2026-06-10)
********************

- 🐛 fix(ci): restore release environment on tag job :pr:`559`
- validate pid range in ``_parse_lock_holder`` :pr:`556` - by :user:`dxbjavid`
- 🔧 ci(release): publish to PyPI on tag push :pr:`557`

********************
 3.29.2 (2026-06-10)
********************

- check hostname in ``is_lock_held_by_us`` :pr:`553` - by :user:`dxbjavid`
- 🔒 fix(soft): harden stale-lock breaking and self-heal malformed locks :pr:`551`
- open marker reads non-blocking to refuse attacker-placed fifo :pr:`549` - by :user:`dxbjavid`

********************
 3.29.1 (2026-06-03)
********************

- 🐛 fix(soft): refuse to follow symlinks when reading the lock file :pr:`548` - by :user:`dxbjavid`
- chore: improve filelock maintenance path :pr:`545` - by :user:`lphuc2250gma`
- chore: improve filelock maintenance path :pr:`544` - by :user:`lphuc2250gma`
- chore: improve filelock maintenance path :pr:`542` - by :user:`lphuc2250gma`
- docs: clarify per-thread scope of :class:`~filelock.FileLock` configuration :pr:`543` - by :user:`Gares95`
- docs: fix API docs of :meth:`~filelock.BaseFileLock.release` :pr:`540` - by :user:`MrAnno`

********************
 3.29.0 (2026-04-19)
********************

- ✨ feat(soft): enable stale lock detection on Windows :pr:`534`
- 🐛 fix(async): use single-thread executor for lock consistency :pr:`533`

********************
 3.28.0 (2026-04-14)
********************

- 🐛 fix(ci): unbreak release workflow, publish to PyPI again :pr:`529`

********************
 3.26.1 (2026-04-09)
********************

- 🐛 fix(asyncio): add ``__exit__`` to :class:`~filelock.BaseAsyncFileLock` and fix ``__del__`` loop handling :pr:`518` - by :user:`naarob`

********************
 3.26.0 (2026-04-06)
********************

- ✨ feat(soft): add PID inspection and lock breaking :pr:`524`
- Remove persist-credentials: false from release job :pr:`520`
- 🔒 ci(workflows): add zizmor security auditing :pr:`517`

********************
 3.25.2 (2026-03-11)
********************

- 🐛 fix(unix): suppress ``EIO`` on close in Docker bind mounts :pr:`513`

********************
 3.25.1 (2026-03-09)
********************

- 🐛 fix(win): restore best-effort lock file cleanup on release :pr:`511`
- 📝 docs(logo): add branded project logo :pr:`507`

********************
 3.25.0 (2026-03-01)
********************

- ✨ feat(async): add :class:`~filelock.AsyncReadWriteLock` :pr:`506`
- Standardize .github files to .yaml suffix
- Move SECURITY.md to .github/SECURITY.md
- Add security policy
- Add permissions to check workflow :pr:`500`

********************
 3.24.3 (2026-02-19)
********************

- 🐛 fix(unix): handle ``ENOENT`` race on FUSE/NFS during acquire :pr:`495`
- 🐛 fix(ci): add trailing blank line after changelog entries :pr:`492`

********************
 3.24.2 (2026-02-16)
********************

- 🐛 fix(rw): close :mod:`sqlite3` cursors and skip :class:`~filelock.SoftFileLock` Windows race :pr:`491`
- 🐛 fix(test): resolve flaky write non-starvation test :pr:`490`
- 📝 docs: restructure using Diataxis framework :pr:`489`

*********************
 3.24.1 (2026-02-15)
*********************

- 🐛 fix(soft): resolve Windows deadlock and test race condition :pr:`488`

*********************
 3.24.0 (2026-02-14)
*********************

- ✨ feat(lock): add ``lifetime`` parameter for lock expiration (#68) :pr:`486`
- ✨ feat(lock): add ``cancel_check`` to :meth:`~filelock.BaseFileLock.acquire` (#309) :pr:`487`
- 🐛 fix(api): detect same-thread self-deadlock :pr:`481`
- ✨ feat(mode): respect POSIX default ACLs (#378) :pr:`483`
- 🐛 fix(win): eliminate lock file race in threaded usage :pr:`484`
- ✨ feat(lock): add ``poll_interval`` to constructor :pr:`482`
- 🐛 fix(unix): auto-fallback to :class:`~filelock.SoftFileLock` on ``ENOSYS`` :pr:`480`

*********************
 3.23.0 (2026-02-14)
*********************

- 📝 docs: move from Unlicense to MIT :pr:`479`
- 📝 docs: add fasteners to similar libraries :pr:`478`

*********************
 3.22.0 (2026-02-14)
*********************

- 🐛 fix(soft): skip stale detection on Windows :pr:`477`
- ✨ feat(soft): detect and break stale locks :pr:`476`

*********************
 3.21.2 (2026-02-13)
*********************

- 🐛 fix: catch :exc:`ImportError` for missing :mod:`sqlite3` C library :pr:`475`

*********************
 3.21.1 (2026-02-12)
*********************

- 🐛 fix: gracefully handle missing :mod:`sqlite3` when importing :class:`~filelock.ReadWriteLock` :pr:`473` - by :user:`bayandin`
- 🐛 fix(ci): make release workflow robust

*********************
 3.21.0 (2026-02-12)
*********************

- 🐛 fix(ci): make release workflow robust
- 👷 ci(release): commit changelog and use release config :pr:`472`
- 👷 ci(release): consolidate to two jobs :pr:`471`
- ✨ feat(unix): delete lock file on release :pr:`408` - by :user:`sbc100`
- ✨ feat(lock): add SQLite-based :class:`~filelock.ReadWriteLock` :pr:`399` - by :user:`leventov`
- 🔧 chore: modernize tooling and bump deps :pr:`470`

**********************
 v3.20.3 (2026-01-09)
**********************

- Fix TOCTOU symlink vulnerability in :class:`~filelock.SoftFileLock` :pr:`465`.

**********************
 v3.20.2 (2026-01-02)
**********************

- Support Unix systems without :data:`os.O_NOFOLLOW` :pr:`463`.

**********************
 v3.20.1 (2025-12-15)
**********************

- Fix TOCTOU symlink vulnerability in lock file creation :pr:`461`.

**********************
 v3.20.0 (2025-10-08)
**********************

- Add Python 3.14 support, drop 3.9 :pr:`448`.
- Add ``tox.toml`` to sdist :pr:`436`.

**********************
 v3.19.1 (2025-08-14)
**********************

- Increase test coverage :pr:`434`.

**********************
 v3.19.0 (2025-08-13)
**********************

- Add support for Python 3.14 :pr:`432`.

**********************
 v3.18.0 (2025-03-11)
**********************

- Support :mod:`fcntl` check on Emscripten :pr:`398`.
- Indicate that locks are exclusive/write locks :pr:`394`.

**********************
 v3.17.0 (2025-01-21)
**********************

- Drop Python 3.8 support :pr:`388`.

**********************
 v3.16.1 (2024-09-17)
**********************

- CI improvements :pr:`362`.

**********************
 v3.16.0 (2024-09-07)
**********************

- Add Python 3.13 to CI :pr:`359`.

**********************
 v3.15.4 (2024-06-22)
**********************

- Pass ``file_lock`` as positional argument :pr:`347`.

**********************
 v3.15.3 (2024-06-19)
**********************

- Fix ``TypeError: _CountedFileLock.__init__() got an unexpected keyword argument`` :pr:`345`.

**********************
 v3.15.2 (2024-06-19)
**********************

- Use a metaclass to implement the singleton pattern :pr:`340`.

**********************
 v3.15.1 (2024-06-12)
**********************

- Restore ``__init__`` method; more robust initialization for singleton locks :pr:`338`.

**********************
 v3.15.0 (2024-06-11)
**********************

- Add asyncio support :pr:`332`.
- Don't re-initialize :class:`~filelock.BaseFileLock` when returning existing singleton instance :pr:`334`.

**********************
 v3.14.0 (2024-04-27)
**********************

- Add ``blocking`` parameter on lock constructor :pr:`325`.

**********************
 v3.13.4 (2024-04-09)
**********************

- Raise error on incompatible singleton ``timeout`` and ``mode`` arguments :pr:`320`.

**********************
 v3.13.3 (2024-03-25)
**********************

- Make singleton class instance dict unique per subclass :pr:`318`.

**********************
 v3.13.2 (2024-03-25)
**********************

- Fix permission denied error when lock file is placed in ``/tmp`` :pr:`317`.

**********************
 v3.13.1 (2023-10-30)
**********************

- Allow users to subclass :class:`~filelock.FileLock` with custom keyword arguments :pr:`284`.

**********************
 v3.13.0 (2023-10-27)
**********************

- Support reentrant locking on lock file path via optional ``is_singleton`` instance :pr:`283`.

**********************
 v3.12.4 (2023-09-13)
**********************

- Change ``typing-extensions`` to be installed only with the ``[typing]`` extra :pr:`276`.

**********************
 v3.12.3 (2023-08-28)
**********************

- Add ``tox.ini`` to sdist :pr:`265`.
- Create parent directories if necessary :pr:`254`.

**********************
 v3.12.2 (2023-06-12)
**********************

- Restore ``if TYPE_CHECKING`` syntax for :class:`~filelock.FileLock` definition :pr:`245`.

**********************
 v3.12.1 (2023-06-10)
**********************

- Add Python 3.12 support :pr:`237`.
- Use ruff for linting :pr:`244`.

**********************
 v3.12.0 (2023-04-18)
**********************

- Make the thread local behavior something the caller can enable/disable via a flag during the lock creation, it's on by
  default.
- Better error handling on Windows.

**********************
 v3.11.0 (2023-04-06)
**********************

- Make the lock thread local.

**********************
 v3.10.7 (2023-03-27)
**********************

- Use :func:`os.fchmod` instead of :func:`os.chmod` to work around bug in PyPy via Anaconda.

**********************
 v3.10.6 (2023-03-25)
**********************

- Enhance the robustness of the try/catch block in _soft.py. by :user:`jahrules`.

**********************
 v3.10.5 (2023-03-25)
**********************

- Add explicit error check as certain UNIX filesystems do not support flock. by :user:`jahrules`.

**********************
 v3.10.4 (2023-03-24)
**********************

- Update os.open to preserve mode= for certain edge cases. by :user:`jahrules`.

**********************
 v3.10.3 (2023-03-23)
**********************

- Fix permission issue - by :user:`jahrules`.

**********************
 v3.10.2 (2023-03-22)
**********************

- Bug fix for using filelock with threaded programs causing undesired file permissions - by :user:`jahrules`.

**********************
 v3.10.1 (2023-03-22)
**********************

- Handle pickle for :class:`~filelock.Timeout` :pr:`203` - by :user:`TheMatt2`.

**********************
 v3.10.0 (2023-03-15)
**********************

- Add support for explicit file modes for lockfiles :pr:`192` - by :user:`jahrules`.

*********************
 v3.9.1 (2023-03-14)
*********************

- Use :func:`time.perf_counter` instead of :func:`time.monotonic` for calculating timeouts.

*********************
 v3.9.0 (2022-12-28)
*********************

- Move build backend to ``hatchling`` :pr:`185` - by :user:`gaborbernat`.

*********************
 v3.8.1 (2022-12-04)
*********************

- Fix mypy does not accept :class:`~filelock.FileLock` as a valid type

*********************
 v3.8.0 (2022-12-04)
*********************

- Bump project dependencies
- Add timeout unit to docstrings
- Support 3.11

*********************
 v3.7.1 (2022-05-31)
*********************

- Make the readme documentation point to the index page

*********************
 v3.7.0 (2022-05-13)
*********************

- Add ability to return immediately when a lock cannot be obtained

*********************
 v3.6.0 (2022-02-17)
*********************

- Fix pylint warning "Abstract class :class:`~filelock.WindowsFileLock` with abstract methods
  instantiated" :pr:`135` - by :user:`vonschultz`
- Fix pylint warning "Abstract class :class:`~filelock.UnixFileLock` with abstract methods instantiated"
  :pr:`135` - by :user:`vonschultz`

*********************
 v3.5.1 (2022-02-16)
*********************

- Use :func:`time.monotonic` instead of :func:`time.time` for calculating timeouts.

*********************
 v3.5.0 (2022-02-15)
*********************

- Enable use as context decorator

*********************
 v3.4.2 (2021-12-16)
*********************

- Drop support for python ``3.6``

*********************
 v3.4.1 (2021-12-16)
*********************

- Add ``stacklevel`` to deprecation warnings for argument name change

*********************
 v3.4.0 (2021-11-16)
*********************

- Add correct spelling of poll interval parameter for :meth:`~filelock.BaseFileLock.acquire` method, raise
  deprecation warning when using the misspelled form :pr:`119` - by :user:`XuehaiPan`.

*********************
 v3.3.2 (2021-10-29)
*********************

- Accept path types (like :class:`pathlib.Path` and :class:`pathlib.PurePath`) in the constructor for :class:`~filelock.FileLock` objects.

*********************
 v3.3.1 (2021-10-15)
*********************

- Add changelog to the documentation :pr:`108` - by :user:`gaborbernat`
- Leave the log level of the ``filelock`` logger as not set (previously was set to warning) :pr:`108` - by
  :user:`gaborbernat`

*********************
 v3.3.0 (2021-10-03)
*********************

- Drop python 2.7 and 3.5 support, add type hints :pr:`100` - by :user:`gaborbernat`
- Document asyncio support - by :user:`gaborbernat`
- fix typo :pr:`98` - by :user:`jugmac00`

*********************
 v3.2.1 (2021-10-02)
*********************

- Improve documentation
- Changed logger name from ``filelock._api`` to ``filelock`` :pr:`97` - by :user:`hkennyv`

*********************
 v3.2.0 (2021-09-30)
*********************

- Raise when trying to acquire in R/O or missing folder :pr:`96` - by :user:`gaborbernat`
- Move lock acquire/release log from INFO to DEBUG :pr:`95` - by :user:`gaborbernat`
- Fix spelling and remove ignored flake8 checks - by :user:`gaborbernat`
- Split main module :pr:`94` - by :user:`gaborbernat`
- Move test suite to pytest :pr:`93` - by :user:`gaborbernat`

*********************
 v3.1.0 (2021-09-27)
*********************

- Update links for new home at tox-dev :pr:`88` - by :user:`hugovk`
- Fixed link to LICENSE file :pr:`63` - by :user:`sharkwouter`
- Adopt tox-dev organization best practices :pr:`87` - by :user:`gaborbernat`
- Ownership moved from :user:`benediktschmitt` to the tox-dev organization (new primary maintainer :user:`gaborbernat`)

**********************
 v3.0.12 (2019-05-18)
**********************

- *fixed* setuptools and twine/warehouse error by making the license only 1 line long
- *update* version for pypi upload
- *fixed* python2 setup error
- *added* test.py module to MANIFEST and made tests available in the setup commands :issue:`48`
- *fixed* documentation thanks to :user:`AnkurTank` :issue:`49`
- Update Trove classifiers for PyPI
- test: Skip test_del on PyPy since it hangs

**********************
 v3.0.10 (2018-11-01)
**********************

- Fix README rendering on PyPI

*********************
 v3.0.9 (2018-10-02)
*********************

- :pr:`38` from cottsay/shebang
- *updated* docs config for older sphinx compatibility
- *removed* misleading shebang from module

*********************
 v3.0.8 (2018-09-09)
*********************

- *updated* use setuptools

*********************
 v3.0.7 (2018-09-09)
*********************

- *fixed* garbage collection (:issue:`37`)
- *fix* travis ci badge (use rst not markdown)
- *changed* travis uri

*********************
 v3.0.6 (2018-08-22)
*********************

- *clean up*
- Fixed unit test for Python 2.7
- Added Travis banner
- Added Travis CI support

*********************
 v3.0.5 (2018-04-26)
*********************

- Corrected the prequel reference

*********************
 v3.0.4 (2018-02-01)
*********************

- *updated* README

*********************
 v3.0.3 (2018-01-30)
*********************

- *updated* readme

*********************
 v3.0.1 (2018-01-30)
*********************

- *updated* README (added navigation)
- *updated* documentation :issue:`22`
- *fix* the :class:`~filelock.SoftFileLock` test was influenced by the test for :class:`~filelock.FileLock`
- *undo* ``cb1d83d`` :issue:`31`

*********************
 v3.0.0 (2018-01-05)
*********************

- *updated* major version number due to :issue:`29` and :issue:`27`
- *fixed* use proper Python3 ``reraise`` method
- Attempting to clean up lock file on Unix after :meth:`~filelock.BaseFileLock.release`

**********************
 v2.0.13 (2017-11-05)
**********************

- *changed* The logger is now acquired when first needed. :issue:`24`

**********************
 v2.0.12 (2017-09-02)
**********************

- correct spelling mistake

**********************
 v2.0.11 (2017-07-19)
**********************

- *added* official support for python 2 :issue:`20`

**********************
 v2.0.10 (2017-06-07)
**********************

- *updated* readme

*********************
 v2.0.9 (2017-06-07)
*********************

- *updated* readme :issue:`19`
- *added* example :pr:`16`
- *updated* readthedocs url
- *updated* change order of the examples (:pr:`16`)

*********************
 v2.0.8 (2017-01-24)
*********************

- Added logging
- Removed unused imports

*********************
 v2.0.7 (2016-11-05)
*********************

- *fixed* :issue:`14` (moved license and readme file to ``MANIFEST``)

*********************
 v2.0.6 (2016-05-01)
*********************

- *changed* unlocking sequence to fix transient test failures
- *changed* threads in tests so exceptions surface
- *added* test lock file cleanup

*********************
 v2.0.5 (2015-11-11)
*********************

- Don't remove file after releasing lock
- *updated* docs

*********************
 v2.0.4 (2015-07-29)
*********************

- *added* the new classes to ``__all__``

*********************
 v2.0.3 (2015-07-29)
*********************

- *added* The :class:`~filelock.SoftFileLock` is now always tested

*********************
 v2.0.2 (2015-07-29)
*********************

- The filelock classes are now always available and have been moved out of the ``if msvrct: ... elif fcntl ... else``
  clauses.

*********************
 v2.0.1 (2015-06-13)
*********************

- fixed :issue:`5`
- *updated* test cases
- *updated* documentation
- *fixed* :issue:`2` which has been introduced with the lock counter

*********************
 v2.0.0 (2015-05-25)
*********************

- *added* default timeout (fixes :issue:`2`)

*********************
 v1.0.3 (2015-04-22)
*********************

- *added* new test case, *fixed* unhandled exception

*********************
 v1.0.2 (2015-04-22)
*********************

- *fixed* a timeout could still be thrown if the lock is already acquired

*********************
 v1.0.1 (2015-04-22)
*********************

- *fixed* :issue:`1`

*********************
 v1.0.0 (2015-04-07)
*********************

- *added* lock counter, *added* unittest, *updated* to version 1
- *changed* filenames
- *updated* version for pypi
- *updated* README, LICENSE (changed format from md to rst)
- *added* MANIFEST to gitignore
- *added* os independent file lock ; *changed* setup.py for pypi
- Update README.md
- initial version
