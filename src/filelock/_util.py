from __future__ import annotations

import os
import secrets
import stat
import sys
from errno import EACCES, EISDIR
from pathlib import Path


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
    if not (file_stat.st_mode & stat.S_IWUSR):
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


def write_all(fd: int, data: bytes) -> None:
    """
    Write every byte of *data* to *fd*, looping over short writes.

    ``os.write`` may write fewer bytes than requested, and a lock file or marker left with a partial holder record
    parses as malformed. A peer then reclaims it after the malformed-lock grace period while this process still holds
    the descriptor, so both own the same logical lock. The published record is coordination state, not diagnostics, so
    it has to be written in full or not published at all.

    :param fd: file descriptor to write to.
    :param data: bytes to write in their entirety.

    :raises OSError: if the underlying write fails before all bytes are written.

    """
    view = memoryview(data)
    written = 0
    while written < len(view):
        count = os.write(fd, view[written:])
        if count == 0:  # pragma: no cover - a blocking regular-file write never reports 0 for a non-empty buffer
            msg = "wrote 0 bytes to the lock file"
            raise OSError(msg)
        written += count


def break_lock_file(lock_file: str, mtime_before: float, ino_before: int) -> None:
    """
    Atomically break a stale lock file judged stale at modification time *mtime_before*.

    Rename the file to a process-private name before unlinking it, so two processes breaking the same lock cannot
    delete each other's work: only one rename of a given inode wins, the loser gets ``OSError``. After the rename,
    re-check the file. A newer modification time, or a different inode than *ino_before*, means a peer recreated the
    lock between the stale decision and the rename, so we grabbed a live file and abort, leaving the renamed file in
    place. A rollback rename is itself racy, the same trade-off as the soft read/write marker break. The inode check
    matters because filesystems with coarse modification-time granularity (NFS, FAT) can give a same-second recreation
    the old mtime, so mtime alone would miss it and unlink a live lock; the inode is the reliable identity, mirroring
    the token re-check in the soft read/write marker break. ``lstat`` avoids following a hostile symlink swapped in
    after the decision.

    The break name carries a random token so it is unguessable and unique per attempt. Without it two breakers in the
    same process share ``<lock>.break.<pid>``, and a second break can rename a recreated live lock onto that path in
    the window between the re-verify ``lstat`` above and the ``unlink`` below, deleting a live lock the inode check
    just approved. A private name keeps anyone else from targeting our break path, matching the soft read/write marker
    break.

    :param lock_file: path to the lock file to break.
    :param mtime_before: modification time observed when the lock was judged stale.
    :param ino_before: inode number observed when the lock was judged stale.

    :raises OSError: if the rename fails (e.g. the file vanished or is not owned in a sticky directory).

    """
    break_path = f"{lock_file}.break.{os.getpid()}.{secrets.token_hex(16)}"
    Path(lock_file).rename(break_path)
    try:
        st_after = os.lstat(break_path)
    except OSError:
        return
    if st_after.st_mtime > mtime_before or st_after.st_ino != ino_before:
        return
    Path(break_path).unlink()


__all__ = [
    "break_lock_file",
    "ensure_directory_exists",
    "raise_on_not_writable_file",
    "write_all",
]
