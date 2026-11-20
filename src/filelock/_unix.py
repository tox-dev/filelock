from __future__ import annotations

from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from importlib.machinery import ModuleSpec

__spec__ = cast("ModuleSpec", __spec__)

__lazy_modules__ = ["contextlib", "pathlib", "warnings", f"{__spec__.parent}._util"]

import os
import sys
import warnings
from contextlib import suppress
from errno import EACCES, EAGAIN, ENOSYS, EWOULDBLOCK
from pathlib import Path
from typing import Final, cast

from ._api import BaseFileLock
from ._util import foreign_owner, open_into, remove_own_file

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
        _FCNTL_UNAVAILABLE: Final[str] = "fcntl is unavailable"

        def lock_fd_nonblocking(_fd: int) -> bool:
            raise OSError(ENOSYS, _FCNTL_UNAVAILABLE)

        def unlock_fd(_fd: int) -> None:
            raise OSError(ENOSYS, _FCNTL_UNAVAILABLE)

    else:
        has_fcntl = True
        # Contention errnos for a nonblocking flock. EAGAIN/EWOULDBLOCK are the usual "held elsewhere" codes; some
        # filesystems report EACCES instead, so treat it as contention too rather than a permanent error.
        _CONTENTION_ERRNOS: Final[frozenset[int]] = frozenset({EACCES, EAGAIN, EWOULDBLOCK})

        def lock_fd_nonblocking(fd: int) -> bool:
            # One nonblocking exclusive flock attempt shared by UnixFileLock and lock_descriptor, so both contend on
            # the same lock and classify errors identically. The caller owns fd; this never closes it.
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exception:
                if exception.errno in _CONTENTION_ERRNOS:
                    return False
                raise
            return True

        def unlock_fd(fd: int) -> None:
            fcntl.flock(fd, fcntl.LOCK_UN)

    def _flock_is_missing(fd: int) -> bool:
        try:
            lock_fd_nonblocking(fd)
        except OSError as exception:
            return exception.errno == ENOSYS
        return False  # a flock the probe won is released when the caller closes fd

    class UnixFileLock(BaseFileLock):
        """
        Uses the :func:`fcntl.flock` to hard lock the lock file on unix systems.

        We leave the lock file in place after release. Unlinking a locked file on Unix splits
        waiters across inodes and breaks mutual exclusion for processes that coordinate via the
        same path.
        """

        def _acquire(self) -> None:
            self._acquire_native()

        def _acquire_native(self) -> None:
            created: list[int] = []
            existing: list[int] = []
            try:
                if (fd := self._open_lock_file(created, existing)) is None:
                    return
                self._mark_descriptor_pending(fd)
                if created:
                    self._apply_explicit_mode(fd)
                try:
                    locked = lock_fd_nonblocking(fd)
                except OSError as exception:
                    if exception.errno != ENOSYS or not self._allows_soft_fallback():
                        raise
                    self._fallback_to_soft_lock()
                    if created:
                        with suppress(OSError):
                            remove_own_file(self._lock_path, fd)
                else:
                    if locked:
                        self._finalize_locked_fd(fd)
                    return
            except BaseException:
                if created and not self.is_locked and self._allows_soft_fallback() and _flock_is_missing(created[0]):
                    with suppress(OSError):
                        remove_own_file(self._lock_path, created[0])
                raise
            finally:
                if (opened := created or existing) and not self.is_locked:
                    try:
                        self._mark_descriptor_released()
                    finally:
                        os.close(opened[0])
            self._acquire()

        def _open_lock_file(self, created: list[int], existing: list[int]) -> int | None:
            # No O_TRUNC: a contender that loses the flock must not truncate the holder's file (#591), and neither may
            # the winner, since a file renamed or hard-linked to the lock path is indistinguishable from a lock file.
            # O_EXCL tells whether this attempt created the inode: only then does it get the explicit mode, and the
            # ENOSYS fallback removes only its own placeholder. O_NONBLOCK: a lease (Linux F_SETLEASE) on the file would
            # otherwise block the open for up to fs.lease-break-time, beyond timeout, blocking=False and cancel_check.
            open_flags = os.O_RDWR | os.O_NONBLOCK
            if (o_nofollow := getattr(os, "O_NOFOLLOW", None)) is not None:
                open_flags |= o_nofollow
            open_mode = self._open_mode()
            try:
                return open_into(created, self._lock_path, open_flags | os.O_CREAT | os.O_EXCL, open_mode)
            except FileExistsError:
                pass
            except FileNotFoundError:
                if self._lock_path and not self._lock_path.endswith(os.sep) and Path(self._lock_path).parent.exists():
                    return None
                raise
            except PermissionError:
                if not Path(self._lock_path).exists():
                    raise
            return self._open_existing_lock_file(existing, open_flags, open_mode)

        def _allows_soft_fallback(self) -> bool:
            return (
                self._settings.fallback_to_soft
                and not self._settings.preserve_lock_file
                and self._settings.on_acquired is None
            )

        def _open_existing_lock_file(self, existing: list[int], open_flags: int, open_mode: int) -> int | None:
            try:
                return open_into(existing, self._lock_path, open_flags, open_mode)
            except FileNotFoundError:
                return None  # unlinked since the create attempt; let the retry loop start over
            except BlockingIOError:
                return None  # a lease is breaking: contention, so the retry loop keeps the deadline
            except PermissionError:
                # A marker another account left is readable but not writable. Without flock, waiting on it only needs
                # reading it; with flock the lock writes its file, so the denial stands.
                try:
                    fd = open_into(existing, self._lock_path, open_flags & ~os.O_RDWR | os.O_RDONLY, open_mode)
                except (FileNotFoundError, BlockingIOError):
                    return None  # unlinked, or leased: the kernel checks permission before the lease
                if _flock_is_missing(fd):
                    return fd
                raise

        def _apply_explicit_mode(self, fd: int) -> None:
            if self.has_explicit_mode:
                with suppress(PermissionError):
                    os.fchmod(fd, self._context.mode)

        def _finalize_locked_fd(self, fd: int) -> bool:
            # Runs with the flock held; the caller closes fd on failure. A concurrent _release() may have unlinked
            # the file between our open() and flock(), which st_nlink 0 shows. That lock guards no name, so report
            # it and let the retry loop start over.
            if (stat_result := os.fstat(fd)).st_nlink == 0:
                return False
            # The owner of a file under a sticky parent may unlink it during the hold. A contender then locks a new
            # inode at the path, and both hold the lock.
            parent = Path(self._lock_path).parent.stat()
            if (owner := foreign_owner(stat_result, parent)) is not None:  # pragma: needs foreign-sticky-tmp
                reason = f"lock file belongs to uid {owner}, not to this process's account or the directory's owner"
                raise PermissionError(EACCES, reason, self._lock_path)
            self._mark_descriptor_owned(fd, (stat_result.st_dev, stat_result.st_ino))
            return True

        def _fallback_to_soft_lock(self: BaseFileLock) -> None:
            # Import lazily: this runs only on the rare flock fallback, and asyncio imports _unix, so a
            # module-level import of it here would cycle.
            from ._soft import (  # ruff:ignore[import-outside-top-level]  # import cycle
                SoftFileLock,
            )

            warnings.warn("flock not supported on this filesystem, falling back to SoftFileLock", stacklevel=2)
            from filelock.asyncio import AsyncSoftFileLock, BaseAsyncFileLock  # ruff:ignore[import-outside-top-level]  # asyncio imports this module

            self.__class__ = AsyncSoftFileLock if isinstance(self, BaseAsyncFileLock) else SoftFileLock

        def _release(self) -> None:
            fd = cast("int", self._context.lock_file_fd)
            # Retain the descriptor until flock succeeds: a failed unlock leaves the kernel lock held, so is_locked
            # must keep reporting held for a retry. Once flock commits, clear held state and close as post-unlock
            # cleanup; a close failure (EIO on FUSE/Docker bind mounts) does not make the kernel lock held again.
            unlock_fd(fd)
            self._mark_descriptor_released()
            self._close_released_fd(fd, default_suppresses=True)


if sys.platform == "win32":  # pragma: win32 cover
    __all__ = ["UnixFileLock", "has_fcntl"]
else:  # pragma: win32 no cover
    __all__ = ["UnixFileLock", "has_fcntl", "lock_fd_nonblocking", "unlock_fd"]
