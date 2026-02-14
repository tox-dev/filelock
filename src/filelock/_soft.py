from __future__ import annotations

import os
import socket
import sys
import time
from contextlib import suppress
from errno import EACCES, EEXIST, EPERM, ESRCH
from pathlib import Path

from ._api import BaseFileLock
from ._util import ensure_directory_exists, raise_on_not_writable_file

_WIN_SYNCHRONIZE = 0x100000
_WIN_ERROR_INVALID_PARAMETER = 87
_WIN_FILE_SHARE_READ = 1
_WIN_FILE_SHARE_WRITE = 2
_WIN_FILE_SHARE_DELETE = 4
_WIN_GENERIC_READ = 0x80000000
_WIN_OPEN_EXISTING = 3
_WIN_INVALID_HANDLE_VALUE = -1


class SoftFileLock(BaseFileLock):
    """
    Portable file lock based on file existence.

    Unlike :class:`UnixFileLock <filelock.UnixFileLock>` and :class:`WindowsFileLock <filelock.WindowsFileLock>`,
    this lock does not use OS-level locking primitives. Instead, it creates the lock file with ``O_CREAT | O_EXCL``
    and treats its existence as the lock indicator. This makes it work on any filesystem but leaves stale lock files
    behind if the process crashes without releasing the lock.

    To mitigate stale locks, the lock file contains the PID and hostname of the holding process. On contention, if the
    holder is on the same host and its PID no longer exists, the stale lock is broken automatically.
    """

    def _acquire(self) -> None:
        raise_on_not_writable_file(self.lock_file)
        ensure_directory_exists(self.lock_file)
        flags = (
            os.O_WRONLY  # open for writing only
            | os.O_CREAT
            | os.O_EXCL  # together with above raise EEXIST if the file specified by filename exists
            | os.O_TRUNC  # truncate the file to zero byte
        )
        if (o_nofollow := getattr(os, "O_NOFOLLOW", None)) is not None:
            flags |= o_nofollow
        try:
            file_handler = os.open(self.lock_file, flags, self._context.mode)
        except OSError as exception:  # re-raise unless expected exception
            if not (
                exception.errno == EEXIST  # lock already exist
                or (exception.errno == EACCES and sys.platform == "win32")  # has no access to this lock
            ):  # pragma: win32 no cover
                raise
            if exception.errno == EEXIST:  # EACCES on Windows means the file is actively held open
                self._try_break_stale_lock()
        else:
            self._write_lock_info(file_handler)
            self._context.lock_file_fd = file_handler

    _STALE_LOCK_MIN_AGE = 2.0

    def _try_break_stale_lock(self) -> None:
        with suppress(OSError):
            # Only probe locks old enough to plausibly be stale — during normal threaded contention the file is
            # sub-second old, and opening it for reading on Windows blocks concurrent deletion even with
            # FILE_SHARE_DELETE (the name stays visible until the last handle closes)
            if time.time() - Path(self.lock_file).stat().st_mtime < self._STALE_LOCK_MIN_AGE:
                return
            content = self._read_lock_info()
            if not content:
                return
            lines = content.strip().splitlines()
            if len(lines) != 2:  # noqa: PLR2004
                return
            pid_str, hostname = lines
            if hostname != socket.gethostname():
                return
            pid = int(pid_str)
            if self._is_process_alive(pid):
                return
            break_path = f"{self.lock_file}.break.{os.getpid()}"
            Path(self.lock_file).rename(break_path)
            Path(break_path).unlink()

    def _read_lock_info(self) -> str:
        if sys.platform == "win32":  # pragma: win32 cover
            import ctypes  # noqa: PLC0415
            from ctypes import wintypes  # noqa: PLC0415

            kernel32 = ctypes.windll.kernel32
            # Open with FILE_SHARE_DELETE so concurrent unlink in _release is not blocked by this read handle
            handle = kernel32.CreateFileW(
                self.lock_file,
                _WIN_GENERIC_READ,
                _WIN_FILE_SHARE_READ | _WIN_FILE_SHARE_WRITE | _WIN_FILE_SHARE_DELETE,
                None,
                _WIN_OPEN_EXISTING,
                0,
                None,
            )
            if handle == _WIN_INVALID_HANDLE_VALUE:
                msg = "CreateFileW failed"
                raise OSError(msg)
            try:
                buf = ctypes.create_string_buffer(256)
                bytes_read = wintypes.DWORD()
                kernel32.ReadFile(handle, buf, 256, ctypes.byref(bytes_read), None)
                return buf.raw[: bytes_read.value].decode("utf-8")
            finally:
                kernel32.CloseHandle(handle)
        fd = os.open(self.lock_file, os.O_RDONLY)
        try:
            return os.read(fd, 256).decode("utf-8")
        finally:
            os.close(fd)

    @staticmethod
    def _is_process_alive(pid: int) -> bool:
        if sys.platform == "win32":  # pragma: win32 cover
            import ctypes  # noqa: PLC0415

            kernel32 = ctypes.windll.kernel32
            handle = kernel32.OpenProcess(_WIN_SYNCHRONIZE, 0, pid)
            if handle:
                kernel32.CloseHandle(handle)
                return True
            return kernel32.GetLastError() != _WIN_ERROR_INVALID_PARAMETER
        try:
            os.kill(pid, 0)
        except OSError as exc:
            if exc.errno == ESRCH:
                return False
            if exc.errno == EPERM:
                return True
            raise
        return True

    @staticmethod
    def _write_lock_info(fd: int) -> None:
        with suppress(OSError):
            os.write(fd, f"{os.getpid()}\n{socket.gethostname()}\n".encode())

    def _release(self) -> None:
        assert self._context.lock_file_fd is not None  # noqa: S101
        os.close(self._context.lock_file_fd)  # the lock file is definitely not None
        self._context.lock_file_fd = None
        with suppress(OSError):  # the file is already deleted and that's what we want
            Path(self.lock_file).unlink()


__all__ = [
    "SoftFileLock",
]
