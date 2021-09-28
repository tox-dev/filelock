import os
import sys
from errno import EACCES, ENOENT, EPERM

from ._api import BaseFileLock

PermissionError = PermissionError if sys.version_info[0] == 3 else OSError


class SoftFileLock(BaseFileLock):
    """Simply watches the existence of the lock file."""

    def _acquire(self):
        open_mode = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_TRUNC
        try:
            fd = os.open(self._lock_file, open_mode)
        except OSError as exception:
            if exception.errno in (EPERM, EACCES, ENOENT):
                raise
            if sys.platform == "win32" and not os.access(self._lock_file, os.W_OK):
                raise PermissionError("Permission denied")
        else:
            self._lock_file_fd = fd

    def _release(self):
        os.close(self._lock_file_fd)
        self._lock_file_fd = None
        try:
            os.remove(self._lock_file)
        # The file is already deleted and that's what we want.
        except OSError:
            pass


__all__ = [
    "SoftFileLock",
]
