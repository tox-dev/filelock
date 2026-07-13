from __future__ import annotations

import os
import sys
from contextlib import suppress
from errno import EACCES
from pathlib import Path
from typing import Final, cast

from ._api import BaseFileLock
from ._util import ensure_directory_exists, raise_on_not_writable_file

if sys.platform == "win32":  # pragma: win32 cover
    import ctypes
    import msvcrt
    from ctypes import wintypes

    _FILE_ATTRIBUTE_REPARSE_POINT: Final[int] = 0x00000400
    _INVALID_FILE_ATTRIBUTES: Final[int] = 0xFFFFFFFF

    _kernel32: Final[ctypes.WinDLL] = ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel32.GetFileAttributesW.argtypes = [wintypes.LPCWSTR]
    _kernel32.GetFileAttributesW.restype = wintypes.DWORD

    class WindowsFileLock(BaseFileLock):
        """
        Uses the :func:`msvcrt.locking` function to hard lock the lock file on Windows systems.

        ``_release`` unlinks the lock file, but the unlink can fail while another thread still holds an
        open handle. A surviving lock file on disk does not affect lock correctness.
        """

        def _acquire(self) -> None:
            raise_on_not_writable_file(self.lock_file)
            ensure_directory_exists(self.lock_file)

            # Refuse a reparse point (symlink/junction) at the lock path to blunt a symlink-swap attack.
            if _is_reparse_point(self.lock_file):
                msg = f"Lock file is a reparse point (symlink/junction): {self.lock_file}"
                raise OSError(msg)

            fd = os.open(self.lock_file, os.O_RDWR | os.O_CREAT, self._open_mode())
            try:
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            except OSError as exception:
                os.close(fd)
                if exception.errno != EACCES:  # EACCES means another holder owns the byte-range lock
                    raise
            else:
                self._context.lock_file_fd = fd

        def _release(self) -> None:
            fd = cast("int", self._context.lock_file_fd)
            self._context.lock_file_fd = None
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            os.close(fd)

            with suppress(OSError):
                Path(self.lock_file).unlink()

    def _is_reparse_point(path: str) -> bool:
        # A missing path reports INVALID_FILE_ATTRIBUTES; the caller creates it, so treat that as
        # not-a-reparse-point and reject only an existing reparse-point attribute.
        if (attrs := _kernel32.GetFileAttributesW(path)) == _INVALID_FILE_ATTRIBUTES:
            return False
        return bool(attrs & _FILE_ATTRIBUTE_REPARSE_POINT)

else:  # pragma: win32 no cover

    class WindowsFileLock(BaseFileLock):
        """Uses the :func:`msvcrt.locking` function to hard lock the lock file on Windows systems."""

        def _acquire(self) -> None:
            raise NotImplementedError

        def _release(self) -> None:
            raise NotImplementedError


__all__ = [
    "WindowsFileLock",
]
