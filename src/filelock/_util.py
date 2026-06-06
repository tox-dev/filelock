from __future__ import annotations

import os
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
    try:  # use stat to do exists + can write to check without race condition
        file_stat = os.stat(filename)  # noqa: PTH116
    except OSError:
        return  # swallow does not exist or other errors

    if file_stat.st_mtime != 0:  # if os.stat returns but modification is zero that's an invalid os.stat - ignore it
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


def break_lock_file(lock_file: str, mtime_before: float) -> None:
    """
    Atomically break a stale lock file that was judged stale at modification time *mtime_before*.

    The file is renamed to a process-private name before being unlinked, so two processes breaking the same lock
    cannot delete each other's work (only one rename of a given inode succeeds; the loser gets ``OSError``). After the
    rename the modification time is re-checked: a value newer than *mtime_before* means a peer recreated the lock
    between the stale decision and the rename, so we grabbed a live file and must abort, leaving the renamed file in
    place rather than rolling back (a rollback rename is itself racy — same trade-off as the soft read/write marker
    break). ``lstat`` is used so a hostile symlink swapped in after the decision is not followed.

    :param lock_file: path to the lock file to break.
    :param mtime_before: modification time observed when the lock was judged stale.

    :raises OSError: if the rename fails (e.g. the file vanished or is not owned in a sticky directory).

    """
    break_path = f"{lock_file}.break.{os.getpid()}"
    Path(lock_file).rename(break_path)
    try:
        mtime_after = os.lstat(break_path).st_mtime
    except OSError:
        return
    if mtime_after > mtime_before:
        return
    Path(break_path).unlink()


__all__ = [
    "break_lock_file",
    "ensure_directory_exists",
    "raise_on_not_writable_file",
]
