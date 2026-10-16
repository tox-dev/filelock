from __future__ import annotations

__lazy_modules__ = ["pathlib", "secrets"]

import os
import secrets
import stat
import sys
from contextlib import contextmanager
from errno import EACCES, EIO, EISDIR
from pathlib import Path
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from collections.abc import Generator


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

    if stat.S_ISDIR(file_stat.st_mode):
        if sys.platform == "win32":  # pragma: win32 cover
            raise PermissionError(EACCES, "Permission denied", filename)
        raise IsADirectoryError(EISDIR, "Is a directory", filename)  # pragma: win32 no cover

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


def ensure_directory_exists(filename: Path | str) -> None:
    """
    Ensure the directory containing the file exists (create it if necessary).

    :param filename: file.

    """
    Path(filename).parent.mkdir(parents=True, exist_ok=True)


@contextmanager
def pin_inode(lock_file: str) -> Generator[int | None]:
    """
    Keep the node at *lock_file* allocated for the length of the block and yield its inode number.

    An inode number names a file only while something keeps the inode allocated: ext4 hands a freed number to the next
    file created, so once a stale marker is unlinked the next holder's marker can carry the number a contender just
    read. A caller that reads the marker inside the block, gives up unless it read the yielded inode, and calls
    :func:`break_lock_file` before leaving can rely on the number.

    :param lock_file: path to the lock file about to be read and perhaps broken.

    :returns: the pinned inode number, or ``None`` where nothing can be pinned: Windows cannot rename a file this
        process holds open, and without ``O_PATH`` a symlink or a socket cannot be opened.

    :raises FileNotFoundError: if the path is gone.

    """
    if sys.platform == "win32":  # pragma: win32 cover
        yield None
    else:  # pragma: win32 no cover
        # O_PATH pins any node, a symlink or socket included, without read permission and without the filesystem seeing
        # an open; elsewhere a nonblocking read-only open pins what it can.
        flags: Final = getattr(os, "O_PATH", os.O_RDONLY | getattr(os, "O_NONBLOCK", 0)) | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(lock_file, flags)
        except FileNotFoundError:
            raise
        except OSError:
            yield None
            return
        try:
            yield os.fstat(fd).st_ino
        finally:
            os.close(fd)


def break_lock_file(lock_file: str, mtime_before: float, ino_before: int) -> None:
    """
    Remove the lock file a caller found stale with modification time *mtime_before* and inode *ino_before*.

    The caller keeps the judged file open, through :func:`pin_inode` or its own descriptor, until we return, so no other
    file can carry *ino_before* meanwhile. Right before renaming we ``lstat`` the lock again and leave it alone if a
    peer recreated it since the caller judged it stale, as Mercurial re-reads a lock under its break lock. We rename
    before unlinking so that, of several processes racing to break the same lock, one takes the file and the rest get
    ``OSError``. Another modification time or another inode on the renamed file means a peer recreated the lock between
    that ``lstat`` and the rename, so we hard-link it back and drop the break name. ``link`` never replaces a name, so a
    marker a third process created after our rename wins and the live one stays under the break name. In the two
    syscalls between rename and link a third process can acquire beside the live holder, a soft lock by creating its
    marker or a StrictSoftFileLock by publishing its sentinel at the empty path. Without ``os.link``, on a filesystem
    that refuses hard links, or where ``os.link`` cannot be told not to follow a symlink, we rename the marker back
    while the path is still free instead. We compare inodes as well as modification times because NFS and FAT store
    coarse modification times, and a peer that recreates the lock within that granularity leaves the old mtime on it.
    Times are compared for equality, since a file's owner can set any time, a future one included. We call ``lstat`` to
    avoid following a symlink a peer swaps in after our stale check, put back only a regular file, and link without
    following symlinks, because a symlink can replace the break name between that ``lstat`` and the link.

    We add a random token to the break name so other processes cannot guess it and two breakers in one process do not
    share ``<lock>.break.<pid>``. With a shared name, the second breaker could rename a recreated live lock onto that
    path between our ``lstat`` and ``unlink``, and our ``unlink`` would delete it.

    :param lock_file: path to the lock file to break.
    :param mtime_before: modification time the caller saw when it judged the lock stale.
    :param ino_before: inode number the caller saw when it judged the lock stale.

    :raises OSError: if the rename, the re-check or the restore fails, for example because the file vanished or another
        user owns it in a sticky directory.

    """
    if (current := os.lstat(lock_file)).st_mtime != mtime_before or current.st_ino != ino_before:
        return
    break_path: Final[str] = f"{lock_file}.break.{os.getpid()}.{secrets.token_hex(16)}"
    Path(lock_file).rename(break_path)
    if (st_after := os.lstat(break_path)).st_mtime == mtime_before and st_after.st_ino == ino_before:
        Path(break_path).unlink()
    elif stat.S_ISREG(st_after.st_mode) and not _linked_back(break_path, lock_file) and not os.path.lexists(lock_file):
        # Unlike link, rename replaces: a marker a third process creates between the check and the rename is lost, the
        # way the live one stays aside when that process wins the same race against link.
        Path(break_path).rename(lock_file)


def _linked_back(break_path: str, lock_file: str) -> bool:
    if _HAS_LINK:  # pragma: needs hard-link
        try:
            os.link(break_path, lock_file, follow_symlinks=False)
        except FileExistsError:
            return True  # a third process took the path after our rename and keeps it
        # A filesystem without hard links refuses, Windows can reject the option and PyPy fails it with EINVAL; a plain
        # link would follow a symlink swapped in under the break name on macOS.
        except (OSError, NotImplementedError, ValueError):
            return False
        Path(break_path).unlink()
        return True
    return False  # pragma: lacks hard-link


def touch(name: str, *, fd: int) -> None:
    # Prefer the already-open, already-verified fd so a peer that swaps a symlink or a different file in at the
    # path after our O_NOFOLLOW read cannot redirect the touch: utime then targets the inode behind the fd.
    # Where the platform cannot utime an fd, fall back to a path-based touch that still refuses to follow a
    # symlink where supported, matching the O_NOFOLLOW reads used elsewhere here.
    if _SUPPORTS_UTIME_FD:  # pragma: needs utime-fd
        os.utime(fd, None)
        return
    os.utime(name, None, follow_symlinks=not _SUPPORTS_UTIME_NOFOLLOW)  # pragma: lacks utime-fd


# Termux/Android CPython ships without os.link.
_HAS_LINK: Final[bool] = hasattr(os, "link")
# Retargeting os.utime to an open fd lets a heartbeat refresh the exact inode it verified instead of whatever the
# pathname now names.
_SUPPORTS_UTIME_FD: Final[bool] = sys.platform != "win32" and os.utime in os.supports_fd
# os.utime follows symlinks unless told not to; not every platform can refuse the follow, so probe support.
_SUPPORTS_UTIME_NOFOLLOW: Final[bool] = os.utime in os.supports_follow_symlinks


__all__ = [
    "break_lock_file",
    "ensure_directory_exists",
    "pin_inode",
    "raise_on_not_writable_file",
    "touch",
    "write_all",
]
