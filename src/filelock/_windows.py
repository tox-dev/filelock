import os

from ._api import BaseFileLock

try:
    import msvcrt
except ImportError:
    msvcrt = None


class WindowsFileLock(BaseFileLock):
    """Uses the :func:`msvcrt.locking` function to hard lock the lock file on windows systems."""

    def _acquire(self):
        open_mode = os.O_RDWR | os.O_CREAT | os.O_TRUNC
        try:
            fd = os.open(self._lock_file, open_mode)
        except OSError:
            pass
        else:
            try:
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            except (OSError, IOError):  # noqa: B014 # IOError is not OSError on python 2
                os.close(fd)
            else:
                self._lock_file_fd = fd

    def _release(self):
        fd = self._lock_file_fd
        self._lock_file_fd = None
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        os.close(fd)

        try:
            os.remove(self._lock_file)
        # Probably another instance of the application hat acquired the file lock.
        except OSError:
            pass


__all__ = [
    "WindowsFileLock",
]
