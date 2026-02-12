from __future__ import annotations

import os
import sys
from contextlib import suppress
from errno import EAGAIN, ENOSYS, EWOULDBLOCK
from pathlib import Path
from typing import cast

from ._api import BaseFileLock
from ._util import ensure_directory_exists

#: a flag to indicate if the fcntl API is available
has_fcntl = False
if sys.platform == "win32":  # pragma: win32 cover

    class UnixFileLock(BaseFileLock):
        """Uses the :func:`fcntl.flock` to hard lock the lock file on unix systems."""

        def _acquire(self) -> None:
            raise NotImplementedError

        def _release(self) -> None:
            raise NotImplementedError

else:  # pragma: win32 no cover
    try:
        import fcntl

        _ = (fcntl.flock, fcntl.LOCK_EX, fcntl.LOCK_NB, fcntl.LOCK_UN)
    except (ImportError, AttributeError):
        pass
    else:
        has_fcntl = True

    class UnixFileLock(BaseFileLock):
        """Uses the :func:`fcntl.flock` to hard lock the lock file on unix systems."""

        def _acquire(self) -> None:
            ensure_directory_exists(self.lock_file)
            open_flags = os.O_RDWR | os.O_TRUNC
            o_nofollow = getattr(os, "O_NOFOLLOW", None)
            if o_nofollow is not None:
                open_flags |= o_nofollow
            open_flags |= os.O_CREAT
            try:
                fd = os.open(self.lock_file, open_flags, self._context.mode)
            except PermissionError:
                # Sticky-bit dirs (e.g. /tmp): O_CREAT fails if the file is owned by another user (#317).
                # Fall back to opening the existing file without O_CREAT.
                if not Path(self.lock_file).exists():
                    raise
                try:
                    fd = os.open(self.lock_file, open_flags & ~os.O_CREAT, self._context.mode)
                except FileNotFoundError:
                    return
            with suppress(PermissionError):  # fchmod fails if the lock file is not owned by this UID
                os.fchmod(fd, self._context.mode)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exception:
                os.close(fd)
                if exception.errno == ENOSYS:
                    msg = "FileSystem does not appear to support flock; use SoftFileLock instead"
                    raise NotImplementedError(msg) from exception
                if exception.errno not in {EAGAIN, EWOULDBLOCK}:
                    raise
            else:
                # The file may have been unlinked by a concurrent _release() between our open() and flock().
                # A lock on an unlinked inode is useless — discard and let the retry loop start fresh.
                if os.fstat(fd).st_nlink == 0:
                    os.close(fd)
                else:
                    self._context.lock_file_fd = fd

        def _release(self) -> None:
            fd = cast("int", self._context.lock_file_fd)
            self._context.lock_file_fd = None
            with suppress(OSError):
                Path(self.lock_file).unlink()
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)


__all__ = [
    "UnixFileLock",
    "has_fcntl",
]
