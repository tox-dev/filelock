# Threat model

## What this project does and where untrusted input enters

filelock is a pure Python library for cross-process mutual exclusion through a lock file. It ships these locks:

- `UnixFileLock` (`src/filelock/_unix.py`): `fcntl.flock` on a lock file opened with `O_NOFOLLOW`. It is what `FileLock`
  resolves to on Unix. When the filesystem returns `ENOSYS` for `flock`, it falls back to `SoftFileLock`.
- `WindowsFileLock` (`_windows.py`): `LockFileEx` on a handle opened through `NtCreateFile` with
  `FILE_OPEN_REPARSE_POINT`. The handle refuses a symlink or junction at the final path component.
- `SoftFileLock` (`_soft.py`): an existence marker created with `O_CREAT | O_EXCL`. The marker records the holder's PID,
  host name and process start token. A contender breaks the marker when the recorded owner is provably gone, or when a
  `lifetime` is configured and the marker is older than it. `MarkerSoftFileLock` (`_marker.py`) and `SoftFileLease`
  (`_lease.py`, a heartbeat lease that may expire) build on it.
- `StrictSoftFileLock` (`_strict.py`): immutable per-owner claims published by hard link under `<path>.filelock/claims`.
  It never reclaims a claim by itself; an operator recovers with `force_break`.
- `ReadWriteLock` (`_read_write.py`): readers and writers coordinated through SQLite transactions on a lock database. On
  POSIX, SQLite connects through `/proc/self/fd/<n>`, or through a private hard link in a `.filelock-*` directory beside
  the database, so that SQLite uses the inode filelock checked.
- `SoftReadWriteLock` (`_soft_rw/`): a read-write lock built on files and hard links, with heartbeats and stale-holder
  eviction.
- Async variants of each (`asyncio.py`, `_async.py`, `_async_read_write.py`, `_soft_rw/_async.py`), and
  `lock_descriptor` (`_descriptor.py`), which locks a descriptor the caller opened.

The library takes no network input. Untrusted input comes from the filesystem and from other local users:

- The lock path and its directory. Applications often put locks in shared directories such as `/tmp`, `/var/tmp`, or a
  group-writable project or cache directory. Another user who can write that directory can create the lock file first,
  plant a symlink, hard link, FIFO, directory or other node at the lock path or at any name the protocol derives from it
  (`<path>.filelock/`, `<path>.break.*`, the SQLite database, `.filelock-*` link directories, soft read-write state
  files), or swap one in between a check and a use.
- Lock file contents. Marker files and claim records are parsed (`_soft.py`, `_marker.py`, `_strict.py`,
  `_soft_rw/_protocol.py`). A record can be forged, truncated, oversized, or crafted to name a PID, host or start token
  that makes a contender believe the owner is gone or alive (`_identity.py`).
- The lock database. `ReadWriteLock` opens a database file that someone else may have created. It refuses a symlink, a
  non-regular file and a WAL-mode database.
- File permissions. Without an explicit `mode`, lock files are created with `0o666` filtered through the umask, and the
  holder applies an explicit `mode` with `fchmod` after it wins the lock. In a shared directory a lock file can be owned
  by another user, or be writable by the group or by everyone.
- Timing. Other processes, including other users' processes, can race every acquire, release, stale break and cleanup.

filelock coordinates cooperating processes. `docs/concepts.rst` ("Trust boundaries and ownership scope") states the
boundary: a process with the holder's UID can rewrite or delete any lock file and is trusted to follow the protocol. The
attacker in scope is therefore a different local user, or content left on disk by one, acting on files and directories
that the victim's lock touches. The defenses the docs promise ("TOCTOU vulnerability" in `docs/concepts.rst`) are
`O_NOFOLLOW` on Unix opens, the reparse-point check on the locked Windows handle, `lstat` followed by `fstat` before any
marker is read, `O_NONBLOCK` against planted FIFOs, and the symlink refusal at the `ReadWriteLock` database path.

## Components that matter most / least

Most:

- Opening, creating and truncating the lock file: `UnixFileLock._open_lock_file` and `_finalize_locked_fd`,
  `_windows._open_non_reparse_fd`, and the `flock` to soft-lock fallback in `_unix.py`.
- Helpers that act on paths: `break_lock_file`, `raise_on_not_writable_file`, `touch` and `ensure_directory_exists` in
  `_util.py`.
- The stale-owner verdict and the marker parsers: `_identity.py` (`owner_is_stale`, `process_start_token`, host names
  and PID namespaces), `_soft.py` (`_read_lock_file`, `_parse_lock_holder`), `_marker.py`.
- `StrictSoftFileLock` claim publication, scanning and removal in `_strict.py`, including `dir_fd` unlinks.
- `ReadWriteLock` database handling in `_read_write.py`: `_open_lock_database`, `_connect_through_descriptor`,
  `_link_beside`, and `_sweep_links_of_the_dead`, which runs `shutil.rmtree` on `.filelock-*` directories it finds
  beside the database.
- The soft read-write lock storage and protocol: `_soft_rw/_storage.py` (opens, renames, `mkdir(0o700)` with a check
  after it) and `_soft_rw/_protocol.py` (snapshot and holder record parsing).
- Reentrancy, thread, fork and cancellation handling in `_api.py` and `asyncio.py`, where a bug can leave two holders.

Least:

- `tasks/` (benchmarks, coverage pragmas, CI helpers), `docs/`, `tests/`, and the Termux setup in `compose.yaml`.
- Exception and warning text, `__repr__`, and the documentation build.

## How to exercise it

The repository is at `/src`. filelock is installed from `/src` in editable mode, with the `test` dependency group, in a
virtual environment at `/opt/venv` on PATH. The build runs as root, and root bypasses file permissions, so cross-user
attacks must run as the unprivileged accounts `victim` and `attacker`, for example
`runuser -u attacker -- python attack.py` against a lock `runuser -u victim -- python holder.py` takes in `/tmp`.

- Test suite: `cd /src && python -m pytest tests`. The symlink, FIFO and race cases live in `tests/test_filelock.py`,
  `tests/test_soft_stale.py`, `tests/test_strict_soft_paths.py`, `tests/test_strict_soft_races.py`,
  `tests/test_read_write.py` and `tests/soft_rw/`. A few tests skip as root because root can write a `0o444` file.
- Compatibility with a legacy client: `FILELOCK_OLD_CLIENT_PATH` points at filelock 3.20.0 in `/opt/filelock-3.20.0`,
  which `tests/test_strict_soft_compat.py` uses.
- Mutual exclusion under contention: `python tasks/verify_filesystem.py [directory]` runs several processes against each
  lock type and fails if two holds overlap.
- A new pytest test, or a pair of scripts run as `victim` and `attacker`, is the best way to show a finding.

The image is Linux. Windows-only code (`_windows.py`, the Windows branches in `_soft.py` and `_read_write.py`) cannot
run here; reason about it from the code and the Windows API documentation.

## How you rate severity

- High: another local user can make the victim's lock open, create, truncate, `chmod`, `utime`, rename, unlink or
  `rmtree` a file or directory other than the intended lock file, with the victim's privileges. Examples: a symlink or
  TOCTOU swap at the lock path, at a derived name or at the SQLite database, or a planted `.filelock-*` directory that
  steers `_sweep_links_of_the_dead`.
- High: a mutual exclusion failure, where two processes both hold the same exclusive lock (or a writer holds beside a
  reader) while each believes it holds it alone, with no `lifetime`, lease expiry or `on_compromise` signal. This
  includes another user's forged marker or claim that makes a live holder look stale.
- Medium: another user can read data from the victim's files through the lock (for example an error message that leaks a
  target's attributes), or make a lock file that the victim created writable by others when the victim asked for a
  restrictive `mode`. A lock file another user created first keeps that user's ownership and mode; that is expected.
- Medium: a crash, hang or unbounded resource use in the victim's process caused by crafted lock file contents or a
  planted node, beyond waiting for the lock. A hang on a planted FIFO is an example.
- Low: denial of service by a local user who holds the lock, creates the lock file first, or leaves a claim behind. This
  is expected of an advisory lock in a directory another user can write.
- Windows-only issues are in scope with the same ratings.

Reports are handled under `.github/SECURITY.md` (Tidelift security contact). Give the filelock version or commit, the
lock class and options, the platform and filesystem, which user runs each process, and a test or script that shows the
problem.

## Anything to leave alone

- Attacks by a process with the same UID as the holder, which can rewrite or delete any lock file. The docs declare this
  out of scope.
- Processes that do not use the lock, and advisory locks not stopping direct access to the protected resource.
- Overlap that the API documents: `SoftFileLock` with `lifetime`, `SoftFileLease` expiry, `SoftReadWriteLock` eviction
  after `stale_threshold`, `break_lock()` and `force_break()`, and legacy clients mixed with `StrictSoftFileLock` as
  `docs/concepts.rst` describes.
- `StrictSoftFileLock` refusing to recover from a crashed holder's claim. It fails closed by design.
- Attacker-controlled directories above the lock's parent. Only the final path component is guarded. On Unix a sticky
  shared directory such as `/tmp` as the lock's parent is in scope; on Windows the docs require the lock directory not
  to be writable by untrusted users.
- `flock` not excluding across NFS client caches, and other network filesystem behavior that the docs list as untested.
- Misuse that filelock reports as an error: an instance used after `fork()`, a WAL-mode database, a directory at the
  lock path.
- Timing-dependent test flakiness on a slow or loaded machine.
