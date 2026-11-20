from __future__ import annotations

from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from collections.abc import Generator
    from importlib.machinery import ModuleSpec

    from ._soft import SoftFileLock

__spec__ = cast("ModuleSpec", __spec__)

__lazy_modules__ = ["pathlib", "secrets", "tempfile", "warnings", f"{__spec__.parent}._util"]

import os
import secrets
import stat
import sys
import tempfile
import warnings
from contextlib import contextmanager, suppress
from errno import EACCES, EAGAIN, ENOSYS, EWOULDBLOCK
from pathlib import Path
from typing import Final, cast

from ._api import (
    BaseFileLock,
    _resolve_lifetime,  # simplint: ignore[private-import-path]  # reuse the base lifetime validator
    canonical,
)
from ._util import exchange_back, exchange_names, exchange_supported, foreign_owner, open_into, write_all

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
            if exception.errno is None:
                raise
            return exception.errno == ENOSYS
        return False  # a flock the probe won is released when the caller closes fd

    class UnixFileLock(BaseFileLock):
        """
        Uses the :func:`fcntl.flock` to hard lock the lock file on unix systems.

        We leave the lock file in place after release. Unlinking a locked file on Unix splits
        waiters across inodes and breaks mutual exclusion for processes that coordinate via the
        same path.
        """

        @BaseFileLock.lifetime.setter
        def lifetime(self, value: float | None) -> None:
            if self._soft_fallback_for_path():
                from ._soft import SoftFileLock  # ruff:ignore[import-outside-top-level]  # importing at module load cycles

                backend = SoftFileLock
            else:
                backend = type(self)
            self._context.lifetime = _resolve_lifetime(value, backend, stacklevel=3)

        def _soft_fallback_for_path(self) -> bool:
            if self._context.claim_root is not None:
                return self._context.soft_fallback
            return canonical(self.lock_file) in self._flockless_roots

        @property
        def pid(self) -> int | None:
            """Read the holder's PID from the soft fallback marker."""
            with self._soft_marker() as soft:
                return soft.pid

        @contextmanager
        def _soft_marker(self) -> Generator[SoftFileLock]:
            if not self._soft_fallback_for_path():
                msg = "marker access requires a soft fallback on the current lock path"
                raise AttributeError(msg)
            with self._soft_lock() as soft:
                yield soft

        @contextmanager
        def _soft_lock(self) -> Generator[SoftFileLock]:
            from ._soft import SoftFileLock  # ruff:ignore[import-outside-top-level]  # importing at module load cycles

            soft = object.__new__(SoftFileLock)
            soft.__dict__ = self.__dict__
            try:
                yield soft
            finally:
                # Its finalizer must not release the real lock's hold.
                soft.__dict__ = {}

        @property
        def is_lock_held_by_us(self) -> bool:
            """Whether the soft fallback marker identifies this process."""
            with self._soft_marker() as soft:
                return soft.is_lock_held_by_us

        def _pin_lock_path(self, canonical: str) -> None:
            super()._pin_lock_path(canonical)
            self._context.soft_fallback = canonical in self._flockless_roots

        def _acquire(self) -> None:
            if self._context.soft_fallback or self._acquire_native():
                with self._soft_lock() as soft:
                    soft._acquire()  # ruff:ignore[private-member-access]  # shares the real lock's state

        def _acquire_native(self) -> bool:
            created: list[int] = []
            existing: list[int] = []
            try:
                fd = self._open_lock_file(created, existing)
                return fd is not None and self._needs_soft_acquire(fd, created)
            except BaseException:
                if created and not self.is_locked:
                    self._discard_interrupted_open(created[0])
                raise
            finally:
                if (opened := created or existing) and self._context.lock_file_fd != opened[0]:
                    try:
                        if self._context.pending_lock_file_fd == opened[0]:
                            self._mark_descriptor_released()
                    finally:
                        os.close(opened[0])

        def _open_lock_file(self, created: list[int], existing: list[int]) -> int | None:
            # No O_TRUNC: a contender that loses the flock must not truncate the holder's file (#591), and neither may
            # the winner, since a file renamed or hard-linked to the lock path is indistinguishable from a lock file.
            # O_EXCL tells whether this attempt created the inode: only then does it get the explicit mode, and the
            # ENOSYS fallback adopts only its own marker. O_NONBLOCK: a lease (Linux F_SETLEASE) on the file would
            # otherwise block the open for up to fs.lease-break-time, beyond timeout, blocking=False and cancel_check.
            open_flags = os.O_RDWR | os.O_NONBLOCK
            if (o_nofollow := getattr(os, "O_NOFOLLOW", None)) is not None:
                open_flags |= o_nofollow
            open_mode = self._open_mode()
            if open_mode & (stat.S_IWGRP | stat.S_IWOTH) and self._allows_soft_fallback():
                if os.path.lexists(self._lock_path):
                    return self._open_existing_lock_file(existing, open_flags, open_mode)
                # A later chmod cannot revoke a descriptor opened before flock reports ENOSYS.
                if self._directory_lacks_flock():
                    self._fallback_to_soft_lock()
                    open_mode &= ~(stat.S_IWGRP | stat.S_IWOTH)
            try:
                return open_into(created, self._lock_path, open_flags | os.O_CREAT | os.O_EXCL, open_mode)
            except OSError as exception:
                if created:
                    raise
                if isinstance(exception, FileNotFoundError):
                    if (
                        self._lock_path
                        and not self._lock_path.endswith(os.sep)
                        and Path(self._lock_path).parent.exists()
                    ):
                        return None
                    raise
                if not isinstance(exception, FileExistsError) and not (
                    isinstance(exception, PermissionError) and Path(self._lock_path).exists()
                ):
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
            except (FileNotFoundError, BlockingIOError, PermissionError) as exception:
                if existing:
                    raise
                if not isinstance(exception, PermissionError):
                    return None  # removed or leased since the create attempt
                # A peer's soft marker only needs read access. A native lock still needs write access.
                try:
                    fd = open_into(existing, self._lock_path, open_flags & ~os.O_RDWR | os.O_RDONLY, open_mode)
                except (FileNotFoundError, BlockingIOError):
                    if existing:
                        raise
                    return None
                if _flock_is_missing(fd):
                    return fd
                raise

        def _directory_lacks_flock(self) -> bool:
            try:
                probe = tempfile.TemporaryFile(  # ruff:ignore[open-file-with-context-handler]  # catch only open errors
                    dir=str(Path(self._lock_path).parent)
                )
            except OSError as exception:
                if exception.errno is None:
                    raise
                # A directory policy may allow the lock name alone. Probe that file after opening it.
                return False
            with probe:
                return _flock_is_missing(probe.fileno())

        def _fallback_to_soft_lock(self) -> None:
            with self._transition_lock:
                if self._lock_path not in self._flockless_roots:
                    warnings.warn("flock not supported on this filesystem, falling back to SoftFileLock", stacklevel=2)
                    self._flockless_roots |= {self._lock_path}
            self._context.soft_fallback = True

        def _needs_soft_acquire(self, fd: int, created: list[int]) -> bool:
            self._mark_descriptor_pending(fd)
            if not self._context.soft_fallback:
                if created:
                    self._apply_explicit_mode(fd)
                try:
                    locked = lock_fd_nonblocking(fd)
                except OSError as exception:
                    if exception.errno != ENOSYS or not self._allows_soft_fallback():
                        raise
                    self._fallback_to_soft_lock()
                else:
                    if locked:
                        self._finalize_locked_fd(fd)
                    return False
            if created:
                self._adopt_created_file(fd, created)
            return not self.is_locked

        def _apply_explicit_mode(self, fd: int) -> None:
            if self.has_explicit_mode:
                with suppress(PermissionError):
                    os.fchmod(fd, self._context.mode)

        def _finalize_locked_fd(self, fd: int) -> None:
            # Runs with the flock held; the caller closes fd on failure. A concurrent _release() may have unlinked
            # the file between our open() and flock(), which st_nlink 0 shows. That lock guards no name, so report
            # it and let the retry loop start over.
            if (stat_result := os.fstat(fd)).st_nlink == 0:
                return
            # The owner of a file under a sticky parent may unlink it during the hold. A contender then locks a new
            # inode at the path, and both hold the lock.
            parent = Path(self._lock_path).parent.stat()
            if (owner := foreign_owner(stat_result, parent)) is not None:  # pragma: needs foreign-sticky-tmp
                reason = f"lock file belongs to uid {owner}, not to this process's account or the directory's owner"
                raise PermissionError(EACCES, reason, self._lock_path)
            self._mark_descriptor_owned(fd, (stat_result.st_dev, stat_result.st_ino))

        def _adopt_created_file(self, fd: int, created: list[int]) -> None:
            marker = os.fstat(fd)
            with self._soft_lock() as soft, suppress(FileNotFoundError):
                if not os.path.samestat(os.lstat(self._lock_path), marker):
                    return
                if marker.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
                    # chmod cannot revoke write access through an already-open descriptor.
                    if exchange_supported():
                        self._exchange_created_file(soft, marker, created)
                    if not self.is_locked and os.path.samestat(os.lstat(self._lock_path), marker):
                        raise PermissionError(EACCES, "soft fallback needs a private marker", self._lock_path)
                    return
                soft._apply_explicit_mode(fd)  # ruff:ignore[private-member-access]  # same lock state
                soft._write_lock_info(fd)  # ruff:ignore[private-member-access]  # same lock state
                if os.path.samestat(os.lstat(self._lock_path), marker):
                    self._mark_descriptor_owned(fd, (marker.st_dev, marker.st_ino))

        def _exchange_created_file(self, soft: SoftFileLock, marker: os.stat_result, created: list[int]) -> None:
            try:
                soft._take_over_by_exchange(marker, None)  # ruff:ignore[private-member-access]  # same lock state
            finally:
                if self.is_locked:
                    # Pop and close in one chain of C calls, so an interrupt cannot leak or double-close the pin.
                    list(map(os.close, map(created.pop, (0,))))

        def _discard_interrupted_open(self, fd: int) -> None:
            if not self._allows_soft_fallback() or not (self._context.soft_fallback or _flock_is_missing(fd)):
                return
            if exchange_supported():
                with suppress(OSError):
                    self._replace_unfinished_marker(fd)

        def _replace_unfinished_marker(self, fd: int) -> None:
            marker = os.fstat(fd)
            if not os.path.samestat(os.lstat(self._lock_path), marker):
                return
            aside = f"{self._lock_path}.break.{os.getpid()}.{secrets.token_hex(16)}"
            opened: list[int] = []
            try:
                pin = open_into(opened, aside, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                write_all(pin, f"filelock/break\n{os.getpid()}\n".encode())
                exchange_names(aside, self._lock_path)
            finally:
                for pin in opened:
                    try:
                        if os.path.samestat(os.lstat(aside), marker):
                            Path(aside).unlink()
                        else:
                            exchange_back(self._lock_path, aside, pin)
                    finally:
                        os.close(pin)

        def _unprobeable_holder(self) -> str | None:
            if not self._context.soft_fallback:
                return None
            with self._soft_lock() as soft:
                return soft._unprobeable_holder()  # ruff:ignore[private-member-access]  # reads the soft marker

        def _try_break_expired_lock(self) -> None:
            if self._context.soft_fallback:
                with self._soft_lock() as soft:
                    soft._try_break_expired_lock()  # ruff:ignore[private-member-access]  # expires the soft marker

        def break_lock(self) -> None:
            """
            Remove this lock's soft marker on its resolved path.

            :raises RuntimeError: if the current path has not entered a soft fallback

            """
            if not self._soft_fallback_for_path():
                msg = "break_lock requires a soft fallback on the current lock path"
                raise RuntimeError(msg)
            with self._soft_lock() as soft:
                soft.break_lock()

        def _release(self) -> None:
            if self._context.soft_fallback:
                with self._soft_lock() as soft:
                    soft._release()  # ruff:ignore[private-member-access]  # releases the soft marker
                return
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
