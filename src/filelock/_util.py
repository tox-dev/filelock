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

    This is done so files that will never be writable can be separated from files that are writable but currently
    locked.

    :param filename: file to check

    :raises OSError: as if the file was opened for writing.

    """
    try:  # use lstat to do exists + can write to check without race condition
        # lstat, not stat: a hostile symlink planted at the lock path would otherwise make this check inspect the
        # link target, so an attacker could turn a contended acquire into a misleading PermissionError /
        # IsADirectoryError and probe the target's attributes. The actual open uses O_NOFOLLOW and refuses the
        # symlink anyway, so reading the link itself here keeps the handling consistent with the rest of the module.
        file_stat = os.lstat(filename)
    except OSError:
        return  # swallow does not exist or other errors

    # No mtime guard: the old `if st_mtime != 0` skip existed for very old NFS/Linux quirks where os.lstat could
    # return an all-zero struct, which it never does today for a file that exists. Skipping the checks when mtime
    # happened to be 0 let a read-only file or a directory in the lock path pass as missing, so acquire() then
    # blocked forever waiting on an open that cannot succeed (or locked a file nothing else can write).
    if not (file_stat.st_mode & stat.S_IWUSR):
        raise PermissionError(EACCES, "Permission denied", filename)

    if stat.S_ISDIR(file_stat.st_mode):
        if sys.platform == "win32":  # pragma: win32 cover
            # On Windows, this is PermissionError
            raise PermissionError(EACCES, "Permission denied", filename)
        else:  # pragma: win32 no cover # noqa: RET506
            # On linux / macOS, this is IsADirectoryError
            raise IsADirectoryError(EISDIR, "Is a directory", filename)


def ensure_directory_exists(filename: Path | str) -> None:
    """
    Ensure the directory containing the file exists (create it if necessary).

    :param filename: file.

    """
    Path(filename).parent.mkdir(parents=True, exist_ok=True)


def break_lock_file(lock_file: str, mtime_before: float, ino_before: int) -> None:
    """
    Atomically break a stale lock file that was judged stale at modification time *mtime_before*.

    The file is renamed to a process-private name before being unlinked, so two processes breaking the same lock
    cannot delete each other's work (only one rename of a given inode succeeds; the loser gets ``OSError``). After the
    rename the file is re-checked: a newer modification time, or a different inode than *ino_before*, means a peer
    recreated the lock between the stale decision and the rename, so we grabbed a live file and must abort, leaving the
    renamed file in place rather than rolling back (a rollback rename is itself racy — same trade-off as the soft
    read/write marker break). The inode check matters because filesystems with coarse modification-time granularity
    (NFS, FAT) can give a same-second recreation the old mtime, so mtime alone would not catch it and a live lock would
    be unlinked; the inode is the reliable identity, mirroring the token re-check in the soft read/write marker break.
    ``lstat`` is used so a hostile symlink swapped in after the decision is not followed.

    The break name carries a random token so it is unguessable and unique per attempt. Without it two breakers in the
    same process share ``<lock>.break.<pid>``, and a second break can rename a freshly recreated live lock onto that
    path in the window between the re-verify ``lstat`` above and the ``unlink`` below, so we would delete a live lock
    the inode check just approved. A private name means nobody else can target our break path, matching the soft
    read/write marker break.

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
]
