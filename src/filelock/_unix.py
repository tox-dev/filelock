from __future__ import annotations

import os
import sys
from typing import cast

from ._api import BaseFileLock

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
    except ImportError:
        pass
    else:
        has_fcntl = True

    class UnixFileLock(BaseFileLock):
        """Uses the :func:`fcntl.flock` to hard lock the lock file on unix systems."""

        def _acquire(self) -> None:
            open_mode = os.O_RDWR | os.O_CREAT
            while True:
                # Make sure we have a non-empty file.
                fd = os.open(self._lock_file, open_mode)
                try:
                    if os.fstat(fd).st_size == 0:
                        os.write(fd, b"Lock files must not be empty, or the Google Drive app will replace them.")
                finally:
                    os.close(fd)

                fd = os.open(self._lock_file, open_mode)
                try:
                    if os.fstat(fd).st_size == 0:
                        # Looks like Google Drive already replaced the file. Try again.
                        os.close(fd)
                        continue
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError:
                    os.close(fd)
                else:
                    self._lock_file_fd = fd
                break

        def _release(self) -> None:
            # Do not remove the lockfile:
            #   https://github.com/tox-dev/py-filelock/issues/31
            #   https://stackoverflow.com/questions/17708885/flock-removing-locked-file-without-race-condition
            fd = cast(int, self._lock_file_fd)
            self._lock_file_fd = None
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)


__all__ = [
    "has_fcntl",
    "UnixFileLock",
]
