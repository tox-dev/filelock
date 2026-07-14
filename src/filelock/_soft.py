from __future__ import annotations

import os
import socket
import stat
import sys
import time
from contextlib import suppress
from errno import EACCES, EEXIST, EPERM, ESRCH
from pathlib import Path
from typing import Final

from ._api import BaseFileLock, _raise_grouped_errors
from ._util import break_lock_file, ensure_directory_exists, raise_on_not_writable_file, write_all

if sys.platform == "win32":  # pragma: win32 cover
    import ctypes
    from ctypes import wintypes

    _KERNEL32: Final[ctypes.WinDLL] = ctypes.WinDLL("kernel32", use_last_error=True)
    _KERNEL32.CloseHandle.argtypes = [wintypes.HANDLE]
    _KERNEL32.CloseHandle.restype = wintypes.BOOL
    _KERNEL32.GetProcessTimes.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
    ]
    _KERNEL32.GetProcessTimes.restype = wintypes.BOOL
    _KERNEL32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _KERNEL32.OpenProcess.restype = wintypes.HANDLE

_WIN_SYNCHRONIZE: Final[int] = 0x100000
_WIN_ERROR_INVALID_PARAMETER: Final[int] = 87
_WIN_INHERIT_HANDLE: Final[bool] = False
_WIN_PROCESS_QUERY_LIMITED_INFORMATION: Final[int] = 0x1000
_MALFORMED_LOCK_AGE_THRESHOLD: Final[float] = 2.0
_MAX_LOCK_FILE_SIZE: Final[int] = 1024
_UNLINK_MAX_RETRIES: Final[int] = 10


class SoftFileLock(BaseFileLock):
    """
    Portable file lock based on file existence.

    Unlike :class:`UnixFileLock <filelock.UnixFileLock>` and :class:`WindowsFileLock <filelock.WindowsFileLock>`, this
    lock does not use OS-level locking primitives. Instead, it creates the lock file with ``O_CREAT | O_EXCL`` and
    treats its existence as the lock indicator. This makes it work on any filesystem but leaves stale lock files behind
    if the process crashes without releasing the lock.

    To mitigate stale locks, the lock file contains the PID and hostname of the holding process. On contention, if the
    holder is on the same host and its PID no longer exists, the stale lock is broken automatically.

    """

    #: Existence locks reclaim by unlinking a pathname, so an age-based lease may break one; a native inode lock cannot.
    _lifetime_supported: bool = True

    #: An existence lock unlinks its marker to release, so it cannot promise to keep the pathname.
    _preserve_lock_file_supported: bool = False

    #: An existence lock keeps protocol state in its marker, so it cannot lend the descriptor to an on_acquired hook.
    _on_acquired_supported: bool = False

    def _acquire(self) -> None:
        raise_on_not_writable_file(self.lock_file)
        ensure_directory_exists(self.lock_file)
        # O_CREAT | O_EXCL makes the create fail with EEXIST when the file already exists, so a successful open
        # means this process now holds the lock.
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_TRUNC
        if (o_nofollow := getattr(os, "O_NOFOLLOW", None)) is not None:
            flags |= o_nofollow
        try:
            fd = os.open(self.lock_file, flags, self._open_mode())
        except OSError as exception:
            if not (
                exception.errno == EEXIST or (exception.errno == EACCES and sys.platform == "win32")
            ):  # pragma: win32 no cover
                raise
            self._try_break_stale_lock()
            return
        self._publish_held_marker(fd)

    def _publish_held_marker(self, fd: int) -> None:
        # Publish held state only once the record is fully on disk. On any failure, including cancellation, close the
        # descriptor and unlink the path only while it still names the file we opened, so a rollback never deletes a
        # successor's marker that replaced ours at the same path after our lease expired.
        identity: tuple[int, int] | None = None
        try:
            identity = _file_identity(os.fstat(fd))
            self._write_lock_info(fd)
        except BaseException:
            os.close(fd)
            with suppress(OSError):
                if identity is not None and _file_identity(os.lstat(self.lock_file)) == identity:
                    Path(self.lock_file).unlink()
            raise
        self._context.lock_file_fd = fd

    def _try_break_stale_lock(self) -> None:
        with suppress(OSError, ValueError):
            content, mtime, ino = _read_lock_file(self.lock_file)
            holder = _parse_lock_holder(content)

            if holder is None:
                # Unparsable: wrong line count, a non-integer PID or creation time, empty, oversized or not UTF-8.
                # Self-heal only once the file is clearly not a half-written fresh lock (a peer between O_EXCL and
                # _write_lock_info), so the brief create-then-write window is never mistaken for a stale lock.
                if time.time() - mtime >= _MALFORMED_LOCK_AGE_THRESHOLD:
                    break_lock_file(self.lock_file, mtime, ino)
                return

            pid, hostname, creation_time = holder
            if hostname != socket.gethostname():
                return

            if self._is_process_alive(pid):
                if sys.platform != "win32" or creation_time is None:  # pragma: win32 no cover
                    return  # same process, or no creation time to disambiguate a recycled PID, so don't evict
                actual = self._get_process_creation_time(pid)  # pragma: win32 cover
                if actual is None or actual == creation_time:  # pragma: win32 cover
                    return  # same process or can't verify, so don't evict
                # PID alive but creation time differs, so the PID was recycled and the lock is stale.

            break_lock_file(self.lock_file, mtime, ino)

    @staticmethod
    def _is_process_alive(pid: int) -> bool:
        if sys.platform == "win32":  # pragma: win32 cover
            handle = _KERNEL32.OpenProcess(_WIN_SYNCHRONIZE, _WIN_INHERIT_HANDLE, pid)
            if handle:
                _KERNEL32.CloseHandle(handle)
                return True
            return ctypes.get_last_error() != _WIN_ERROR_INVALID_PARAMETER
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
    def _get_process_creation_time(pid: int) -> int | None:
        """Return the process creation FILETIME as an integer on Windows, ``None`` otherwise."""
        if sys.platform != "win32":  # pragma: win32 no cover
            return None
        handle = _KERNEL32.OpenProcess(_WIN_PROCESS_QUERY_LIMITED_INFORMATION, _WIN_INHERIT_HANDLE, pid)
        if not handle:
            return None
        try:
            creation = wintypes.FILETIME()
            exit_time = wintypes.FILETIME()
            kernel_time = wintypes.FILETIME()
            user_time = wintypes.FILETIME()
            if not _KERNEL32.GetProcessTimes(
                handle,
                ctypes.byref(creation),
                ctypes.byref(exit_time),
                ctypes.byref(kernel_time),
                ctypes.byref(user_time),
            ):
                return None
        finally:
            _KERNEL32.CloseHandle(handle)
        return (creation.dwHighDateTime << 32) | creation.dwLowDateTime

    @staticmethod
    def _write_lock_info(fd: int) -> None:
        # No suppression: a write failure must reach the acquisition rollback so it never publishes a half-written
        # marker as held state.
        info = f"{os.getpid()}\n{socket.gethostname()}\n"
        if sys.platform == "win32" and (ct := SoftFileLock._get_process_creation_time(os.getpid())) is not None:
            info += f"{ct}\n"
        write_all(fd, info.encode())

    @property
    def pid(self) -> int | None:
        """
        The PID of the process holding this lock, read from the lock file.

        :returns: the PID as an integer, or ``None`` if the lock file does not exist or cannot be parsed

        """
        with suppress(OSError, ValueError):
            holder = _parse_lock_holder(_read_lock_file(self.lock_file)[0])
            if holder is not None:
                return holder[0]
        return None

    @property
    def is_lock_held_by_us(self) -> bool:
        """
        Whether this lock is held by the current process.

        :returns: ``True`` if the lock file exists and names the current process's PID and hostname

        """
        with suppress(OSError, ValueError):
            holder = _parse_lock_holder(_read_lock_file(self.lock_file)[0])
            if holder is not None:
                pid, hostname, _ = holder
                return pid == os.getpid() and hostname == socket.gethostname()
        return False

    def break_lock(self) -> None:
        """Forcibly break the lock by removing the lock file, regardless of who holds it."""
        with suppress(OSError):
            Path(self.lock_file).unlink()

    def _release(self) -> None:
        fd = self._context.lock_file_fd
        assert fd is not None  # noqa: S101
        # Capture the held file's identity before closing so cleanup can refuse to unlink a successor's marker. A
        # supported lifetime lease lets a peer break our expired marker and create its own at this path before we
        # release; unlinking by path alone would then delete the successor's lock.
        identity: tuple[int, int] | None = None
        with suppress(OSError):
            identity = _file_identity(os.fstat(fd))
        # A failed close may already have released and recycled the descriptor number. Relinquish it before the one
        # close attempt so no later release can close an unrelated descriptor that reused the same integer.
        self._context.lock_file_fd = None
        try:
            self._close_released_fd(fd, default_suppresses=False)
        # Marker cleanup must also run for control-flow exceptions, and both failures must remain observable.
        except BaseException as close_error:
            try:
                self._unlink_held_marker(identity)
            except BaseException as cleanup_error:  # noqa: BLE001  # preserve control-flow cleanup failures
                _raise_grouped_errors(
                    "lock descriptor close and marker cleanup both failed",
                    close_error,
                    cleanup_error,
                )
            raise
        self._unlink_held_marker(identity)

    def _unlink_held_marker(self, identity: tuple[int, int] | None) -> None:
        if identity is None:
            return
        if sys.platform == "win32":
            self._windows_unlink_if_ours(identity)
        else:
            with suppress(OSError):
                if _file_identity(os.lstat(self.lock_file)) == identity:
                    Path(self.lock_file).unlink()

    def _windows_unlink_if_ours(self, identity: tuple[int, int]) -> None:
        retry_delay = 0.001
        for attempt in range(_UNLINK_MAX_RETRIES):
            # Windows doesn't immediately release file handles after close, causing EACCES/EPERM on unlink. Recheck
            # identity each attempt: a failed unlink leaves a window for a successor to replace the marker at this path.
            try:
                if _file_identity(os.lstat(self.lock_file)) != identity:
                    return
                Path(self.lock_file).unlink()
            except OSError as exc:  # noqa: PERF203
                if exc.errno not in {EACCES, EPERM}:
                    return
                if attempt < _UNLINK_MAX_RETRIES - 1:
                    time.sleep(retry_delay)
                    retry_delay *= 2
            else:
                return


def _file_identity(st: os.stat_result) -> tuple[int, int]:
    # (st_dev, st_ino) names the concrete inode behind a path, so a marker recreated at the same pathname after an
    # expired lease reads as a different file. CPython populates both on Windows from the volume serial and file index.
    return st.st_dev, st.st_ino


def _read_lock_file(path: str) -> tuple[str | None, float, int]:
    # A legitimate lock file is always a regular file. Classify the path with lstat first, so any other node (symlink,
    # FIFO, socket, device) is reported as a malformed lock the caller can evict, without an os.open that would follow
    # a symlink, stall on a FIFO, or fail on a socket and leave acquisition wedged. The mtime and inode still flow back
    # for the identity-checked stale break. lstat, not stat, so a hostile symlink is never followed onto its target.
    st = os.lstat(path)
    if not stat.S_ISREG(st.st_mode):
        return None, st.st_mtime, st.st_ino
    # Re-check on the opened handle: O_NOFOLLOW refuses a symlink swapped in after the lstat, O_NONBLOCK stops a FIFO
    # swapped in from stalling the open, and the fstat catches any other non-regular replacement race before we read.
    # The capped read stops a huge regular file (e.g. one filled from /dev/zero) from exhausting memory.
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):  # pragma: no cover  # only a non-regular node swapped in after the lstat
            return None, st.st_mtime, st.st_ino
        data = os.read(fd, _MAX_LOCK_FILE_SIZE + 1)
    finally:
        os.close(fd)
    if len(data) <= _MAX_LOCK_FILE_SIZE:
        with suppress(UnicodeDecodeError):
            return data.decode("utf-8"), st.st_mtime, st.st_ino
    return None, st.st_mtime, st.st_ino


def _parse_lock_holder(content: str | None) -> tuple[int, str, int | None] | None:
    # A well-formed lock file is "<pid>\n<hostname>\n" with an optional "<creation_time>\n" third line on Windows.
    # Anything else (wrong line count, a non-integer PID or creation time, empty or unreadable content) is
    # unparsable; returning None lets the caller treat it as a malformed lock to self-heal rather than a holder.
    if not content or len(lines := content.strip().splitlines()) not in {2, 3}:
        return None
    try:
        pid = int(lines[0])
        creation_time = int(lines[2]) if len(lines) == 3 else None  # noqa: PLR2004
    except ValueError:
        return None
    # A pid outside the valid range is a malformed lock, not a holder. Without this, a non-positive pid
    # reaches os.kill() where 0 / -1 mean "the caller's own process group / every process" so a dead
    # holder reads as alive and the lock is never reclaimed, while an oversized pid raises OverflowError
    # (not OSError/ValueError) out of the self-heal path. _parse_marker_bytes already enforces this range.
    if not 1 <= pid <= 2**31 - 1:
        return None
    return pid, lines[1], creation_time


__all__ = [
    "SoftFileLock",
]
