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

    _GENERIC_READ: Final[int] = 0x80000000
    _GENERIC_WRITE: Final[int] = 0x40000000
    _FILE_SHARE_READ_WRITE: Final[int] = (
        0x00000001 | 0x00000002
    )  # read | write; matches os.open (_SH_DENYNO), no delete
    _OPEN_ALWAYS: Final[int] = 4  # open the file if it exists, create it otherwise
    _FILE_ATTRIBUTE_READONLY: Final[int] = 0x00000001
    _FILE_ATTRIBUTE_REPARSE_POINT: Final[int] = 0x00000400
    _FILE_FLAG_OPEN_REPARSE_POINT: Final[int] = 0x00200000  # open the reparse point itself instead of following it
    _INVALID_HANDLE_VALUE: Final[int] = cast("int", wintypes.HANDLE(-1).value)  # non-null handle, never None
    _ERROR_ACCESS_DENIED: Final[int] = 5
    _ERROR_SHARING_VIOLATION: Final[int] = 32
    _OWNER_WRITE: Final[int] = 0o200

    _kernel32: Final[ctypes.WinDLL] = ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    _kernel32.CreateFileW.restype = wintypes.HANDLE
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel32.CloseHandle.restype = wintypes.BOOL

    class _BY_HANDLE_FILE_INFORMATION(ctypes.Structure):  # noqa: N801  # mirrors the Win32 struct name
        _fields_ = (
            ("dwFileAttributes", wintypes.DWORD),
            ("ftCreationTime", wintypes.FILETIME),
            ("ftLastAccessTime", wintypes.FILETIME),
            ("ftLastWriteTime", wintypes.FILETIME),
            ("dwVolumeSerialNumber", wintypes.DWORD),
            ("nFileSizeHigh", wintypes.DWORD),
            ("nFileSizeLow", wintypes.DWORD),
            ("nNumberOfLinks", wintypes.DWORD),
            ("nFileIndexHigh", wintypes.DWORD),
            ("nFileIndexLow", wintypes.DWORD),
        )

    _kernel32.GetFileInformationByHandle.argtypes = [wintypes.HANDLE, ctypes.POINTER(_BY_HANDLE_FILE_INFORMATION)]
    _kernel32.GetFileInformationByHandle.restype = wintypes.BOOL

    class WindowsFileLock(BaseFileLock):
        """
        Uses the :func:`msvcrt.locking` function to hard lock the lock file on Windows systems.

        Lock file cleanup: Windows attempts to delete the lock file after release, but deletion is
        not guaranteed in multi-threaded scenarios where another thread holds an open handle. The lock
        file may persist on disk, which does not affect lock correctness.
        """

        def _acquire(self) -> None:
            raise_on_not_writable_file(self.lock_file)
            ensure_directory_exists(self.lock_file)

            # The reparse test is bound to the opened handle, so a symlink or junction swapped in cannot defeat it
            # through a check-then-open TOCTOU race.
            fd = _open_non_reparse_fd(self.lock_file, self._open_mode())
            if fd is None:
                return  # access/sharing contention on open; let the retry loop try again
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
            # Retain the descriptor until the OS unlock succeeds: if msvcrt.locking raises, the byte-range lock is
            # still held, so is_locked must keep reporting held rather than losing the fd. Only after the unlock
            # commits do close and unlink run as post-unlock cleanup; their failure cannot make the lock held again.
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            self._context.lock_file_fd = None
            os.close(fd)
            with suppress(OSError):
                Path(self.lock_file).unlink()

    def _open_non_reparse_fd(path: str, mode: int) -> int | None:
        """
        Open *path* for locking while refusing reparse points, bound to the handle actually locked.

        The file is opened with ``FILE_FLAG_OPEN_REPARSE_POINT`` so a symlink or junction planted at the path is not
        followed, and the reparse decision is read from *that* handle via ``GetFileInformationByHandle`` rather than
        from a prior pathname query. Reading the held handle closes the check-then-open race: an attacker cannot swap
        the path between validation and use because both now act on the same handle. Share mode omits delete so a peer
        cannot unlink or rename the file out from under a live holder, matching ``os.open``'s ``_SH_DENYNO``.

        The flag only guards the final path component; Windows still follows reparse points in intermediate
        directories. This assumes the lock file sits in a lock directory untrusted users cannot modify — a path with
        attacker-controlled parent directories would need component-by-component handle validation.

        :param path: the lock file path.
        :param mode: the permission mode; as ``os.open`` does on Windows, a cleared owner-write bit creates the file
            read-only. The attribute only takes effect when the file is created, not when an existing one is opened.

        :returns: a file descriptor owning the opened handle, or ``None`` on a sharing violation or a delete-pending
            access denial the caller should treat as contention and retry.

        :raises OSError: if the path resolves to a reparse point, or the open fails for any other reason, raised with
            its real Windows error code rather than masked as contention.

        """
        # Emit the audit event os.open would, so consumers watching "open" still see the path-level open and can veto.
        sys.audit("open", path, None, os.O_RDWR | os.O_CREAT)
        flags_and_attributes = _FILE_FLAG_OPEN_REPARSE_POINT
        if not mode & _OWNER_WRITE:
            flags_and_attributes |= _FILE_ATTRIBUTE_READONLY
        handle = _kernel32.CreateFileW(
            path,
            _GENERIC_READ | _GENERIC_WRITE,
            _FILE_SHARE_READ_WRITE,
            None,
            _OPEN_ALWAYS,
            flags_and_attributes,
            None,
        )
        if handle == _INVALID_HANDLE_VALUE:
            err = ctypes.get_last_error()
            if err in {_ERROR_SHARING_VIOLATION, _ERROR_ACCESS_DENIED}:
                # Both are contention, not permanent failure. A conflicting share mode reports a sharing violation. A
                # lock file another holder just unlinked lingers in Windows' delete-pending state, where CreateFileW
                # returns access-denied until the last handle closes; os.open reports the same case as EACCES and the
                # backend has always retried it. Permanent denials (a read-only file, a directory at the path) are
                # already rejected up front by raise_on_not_writable_file.
                return None
            # Raise the real Windows failure with the pathname. A second os.open() to reshape the exception could
            # observe a swapped path (a fresh TOCTOU) and would mask the true error, so map the captured code directly:
            # winerror selects the right OSError subclass (FileNotFoundError for a missing path, and so on).
            raise OSError(None, ctypes.FormatError(err).strip(), path, err)

        info = _BY_HANDLE_FILE_INFORMATION()
        if not _kernel32.GetFileInformationByHandle(handle, ctypes.byref(info)):
            err = ctypes.get_last_error()
            _kernel32.CloseHandle(handle)
            raise ctypes.WinError(err)
        if info.dwFileAttributes & _FILE_ATTRIBUTE_REPARSE_POINT:
            _kernel32.CloseHandle(handle)
            msg = f"Lock file is a reparse point (symlink/junction): {path}"
            raise OSError(msg)

        try:
            # O_NOINHERIT mirrors os.open on Windows: the lock fd must not leak into child processes.
            return msvcrt.open_osfhandle(handle, os.O_RDWR | os.O_NOINHERIT)
        except BaseException:  # open_osfhandle audits too; a hook raising anything must not leak the handle
            _kernel32.CloseHandle(handle)
            raise

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
