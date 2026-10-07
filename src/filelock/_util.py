from __future__ import annotations

import os
import secrets
import stat
import sys
from errno import EACCES, EIO, EISDIR
from pathlib import Path
from typing import Final


def write_all(fd: int, data: bytes) -> None:
    """
    Write the whole buffer to *fd*, looping over the short writes POSIX lets ``os.write`` make.

    A caller that stopped after one short write would leave a truncated marker behind. A peer that reads between two
    writes sees a prefix of the record; each caller has to tolerate that. Lock records need not survive a power loss, so
    we skip ``fsync``.

    :param fd: file descriptor open for writing.
    :param data: bytes to write in full.

    :raises OSError: if a write reports zero progress before the record is complete.

    """
    remaining = memoryview(data)
    while remaining:
        if (written := os.write(fd, remaining)) == 0:
            raise OSError(EIO, "os.write wrote 0 bytes before the record was complete")
        remaining = remaining[written:]


def raise_on_not_writable_file(filename: str) -> None:
    """
    Raise an exception if attempting to open the file for writing would fail.

    Separates files that can never be written from files that are writable but currently locked.

    :param filename: file to check

    :raises OSError: as if the file was opened for writing.

    """
    try:
        # lstat, not stat: settles exists-and-writable in one syscall, and a hostile symlink at the lock path would
        # make stat inspect the link target, letting an attacker turn a contended acquire into a misleading
        # PermissionError / IsADirectoryError and probe that target's attributes. The real open passes O_NOFOLLOW and
        # refuses the symlink anyway.
        file_stat = os.lstat(filename)
    except OSError:
        return  # does not exist, or an error the caller cannot act on

    # No mtime guard: the old `if st_mtime != 0` skip covered NFS/Linux quirks where os.lstat returned an all-zero
    # struct, which it no longer does. Skipping on mtime 0 let a read-only file or a directory at the lock path pass
    # as missing, so acquire() blocked forever on an open that cannot succeed.
    # Match open() credentials where supported; group permissions and ACLs can grant access without S_IWUSR.
    if not (file_stat.st_mode & stat.S_IWUSR) and not os.access(
        filename, os.W_OK, effective_ids=os.access in os.supports_effective_ids
    ):
        try:
            os.lstat(filename)
        except FileNotFoundError:
            # A holder may unlink its marker between lstat() and access(); let the caller attempt creation.
            return
        raise PermissionError(EACCES, "Permission denied", filename)

    if stat.S_ISDIR(file_stat.st_mode):
        if sys.platform == "win32":  # pragma: win32 cover
            raise PermissionError(EACCES, "Permission denied", filename)
        raise IsADirectoryError(EISDIR, "Is a directory", filename)  # pragma: win32 no cover


def ensure_directory_exists(filename: Path | str) -> None:
    """
    Ensure the directory containing the file exists (create it if necessary).

    :param filename: file.

    """
    Path(filename).parent.mkdir(parents=True, exist_ok=True)


def break_lock_file(lock_file: str, mtime_before: float, ino_before: int) -> None:
    """
    Remove the lock file a caller found stale with modification time *mtime_before* and inode *ino_before*.

    We rename before unlinking so that, of several processes racing to break the same lock, one takes the file and the
    rest get ``OSError``. If we find a newer modification time or another inode on the renamed file, a peer recreated
    the lock after we judged it stale, and we leave that live file under the break name. Its holder keeps running with
    no file at the lock path, so a third process can acquire the lock alongside it. We do not rename the file back,
    because on POSIX that would replace any marker a third process created after our rename. StrictSoftFileLock has no
    such race. Its sole way to remove another process's claim is an operator's ``force_break``. We compare inodes as
    well as modification times because NFS and FAT store modification times at coarse granularity, and a peer that
    recreates the lock within that granularity leaves the old mtime on it. We call ``lstat`` to avoid following a
    symlink a peer swaps in after our stale check.

    We add a random token to the break name so other processes cannot guess it and two breakers in one process do not
    share ``<lock>.break.<pid>``. With a shared name, the second breaker could rename a recreated live lock onto that
    path between our ``lstat`` and ``unlink``, and our ``unlink`` would delete it.

    :param lock_file: path to the lock file to break.
    :param mtime_before: modification time the caller saw when it judged the lock stale.
    :param ino_before: inode number the caller saw when it judged the lock stale.

    :raises OSError: if the rename or the re-check fails, for example because the file vanished or another user owns it
        in a sticky directory.

    """
    break_path: Final[str] = f"{lock_file}.break.{os.getpid()}.{secrets.token_hex(16)}"
    Path(lock_file).rename(break_path)
    if (st_after := os.lstat(break_path)).st_mtime > mtime_before or st_after.st_ino != ino_before:
        return
    Path(break_path).unlink()


def touch(name: str, *, fd: int) -> None:
    # Prefer the already-open, already-verified fd so a peer that swaps a symlink or a different file in at the
    # path after our O_NOFOLLOW read cannot redirect the touch: utime then targets the inode behind the fd.
    # Where the platform cannot utime an fd, fall back to a path-based touch that still refuses to follow a
    # symlink where supported, matching the O_NOFOLLOW reads used elsewhere here.
    if _SUPPORTS_UTIME_FD:  # pragma: needs utime-fd
        os.utime(fd, None)
        return
    os.utime(name, None, follow_symlinks=not _SUPPORTS_UTIME_NOFOLLOW)  # pragma: lacks utime-fd


# Retargeting os.utime to an open fd lets a heartbeat refresh the exact inode it verified instead of whatever the
# pathname now names.
_SUPPORTS_UTIME_FD: Final[bool] = sys.platform != "win32" and os.utime in os.supports_fd
# os.utime follows symlinks unless told not to; not every platform can refuse the follow, so probe support.
_SUPPORTS_UTIME_NOFOLLOW: Final[bool] = os.utime in os.supports_follow_symlinks


__all__ = [
    "break_lock_file",
    "ensure_directory_exists",
    "raise_on_not_writable_file",
    "touch",
    "write_all",
]
