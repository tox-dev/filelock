import os

from ._api import BaseFileLock


class SoftFileLock(BaseFileLock):
    """Simply watches the existence of the lock file."""

    def _acquire(self):
        open_mode = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_TRUNC
        try:
            fd = os.open(self._lock_file, open_mode)
        except OSError:
            pass
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
