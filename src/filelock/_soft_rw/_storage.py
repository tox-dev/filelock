"""The :class:`~._protocol.Files` implementation over :mod:`os`."""

from __future__ import annotations

__lazy_modules__ = ["filelock._soft_rw._protocol", "filelock._strict", "filelock._util", "pathlib"]

import os
import stat
import sys
import time
from contextlib import contextmanager, suppress
from errno import ELOOP, ENOENT, ENOTDIR, ENXIO, ESTALE
from pathlib import Path
from typing import TYPE_CHECKING, Final

from filelock._soft_rw._protocol import _GENERATIONS_DIRECTORY, _HOLDERS_DIRECTORY, _MAX_RECORD_SIZE
from filelock._strict import _link_no_follow, _raise_if_hard_links_unsupported
from filelock._util import ensure_directory_exists, foreign_directory_owner, write_all

if TYPE_CHECKING:
    from collections.abc import Generator

_O_NOFOLLOW: Final[int] = getattr(os, "O_NOFOLLOW", 0)
#: Probed once at import, as in :mod:`filelock._strict`: a per-call check flips the moment a test mocks the function.
#: os.replace shares os.rename's implementation, which is the one the set lists.
_SUPPORTS_DIR_FD: Final[bool] = (
    hasattr(os, "link")
    and {os.link, os.open, os.stat, os.mkdir, os.rename, os.unlink} <= os.supports_dir_fd
    and os.scandir in os.supports_fd
)
#: Windows opens descriptors in text mode by default, which rewrites newlines; every record here is exact bytes.
_O_BINARY: Final[int] = getattr(os, "O_BINARY", 0)
#: ESTALE is what an NFS client reports for a file a peer unlinked out from under its cached handle: gone, not broken.
_MISSING_ERRNOS: Final[frozenset[int]] = frozenset({ENOENT, ESTALE})
#: Windows refuses an open with EACCES while another process is deleting the file or holds it open without sharing;
#: both clear within moments, so an open is retried this long before the refusal is taken as real.
_WINDOWS_OPEN_GRACE: Final[float] = 0.5


class OsFiles:
    """Record files under a lock's protocol root, opened without following symlinks and owned by this UID alone."""

    def __init__(self, lock_file: str) -> None:
        self._lock_file = lock_file
        self._prepared = False

    @staticmethod
    def read(path: str) -> bytes | None:
        # O_NONBLOCK keeps an open of a FIFO planted at the path from blocking; the regular-file check below rejects it.
        if (fd := _open(path, os.O_RDONLY | _O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0) | _O_BINARY)) is None:
            return None
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                return b""
            chunks: list[bytes] = []
            remaining = _MAX_RECORD_SIZE + 1
            while remaining > 0 and (chunk := os.read(fd, remaining)):
                chunks.append(chunk)
                remaining -= len(chunk)
            return b"".join(chunks)
        finally:
            os.close(fd)

    @classmethod
    def create(cls, path: str, data: bytes, *, durable: bool = False) -> tuple[int, int]:
        with _relative(path) as (name, dir_fd):
            fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_NOFOLLOW | _O_BINARY, 0o600, dir_fd=dir_fd)
        try:
            write_all(fd, data)
            if durable:
                os.fsync(fd)
            created = os.fstat(fd)
        except BaseException:
            os.close(fd)
            # The name is unique to this call, so removing it cannot take a record from anyone else.
            cls.unlink(path)
            raise
        os.close(fd)
        return created.st_dev, created.st_ino

    def link(self, source: str, target: str) -> bool:
        failure: NotImplementedError | OSError | None = None
        with _relative(source) as (source_name, source_fd), _relative(target) as (target_name, target_fd):
            try:
                _link_no_follow(source_name, target_name, src_dir_fd=source_fd, dst_dir_fd=target_fd)
            except FileExistsError:
                pass
            except (NotImplementedError, OSError) as error:
                _raise_if_hard_links_unsupported(self._lock_file, error)
                failure = error
            # Whatever the call reported, the file's identity decides: an NFS retransmit can turn a link that landed
            # into an error, and an EEXIST can be this very link answered twice. Any other error on a link that did not
            # land is a real fault, not a peer winning the race, and retrying it would spin.
            if (identity := _identity(source_name, source_fd)) is not None and identity == _identity(
                target_name, target_fd
            ):
                return True
        if failure is not None:
            raise failure
        return False

    @staticmethod
    def replace(source: str, target: str) -> bool:
        try:
            with _relative(source) as (source_name, source_fd), _relative(target) as (target_name, target_fd):
                os.replace(source_name, target_name, src_dir_fd=source_fd, dst_dir_fd=target_fd)
        except PermissionError:
            # Windows refuses to rename over a file a reader holds open; callers tolerate a target one rename behind.
            return False
        return True

    @staticmethod
    def overwrite(path: str, data: bytes, identity: tuple[int, int]) -> bool:
        # O_NONBLOCK keeps an open of a FIFO put at the path from blocking, as in read; with no reader it fails ENXIO.
        try:
            fd = _open(path, os.O_WRONLY | _O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0) | _O_BINARY)
        except OSError as error:
            if error.errno != ENXIO:
                raise
            return False  # pragma: needs fifo
        if fd is None:
            return False
        try:
            # O_NOFOLLOW refuses a symlink, not a hard link or another file renamed over the name, and a write through
            # either lands in a file that is not the record. Nothing links a holder record, so a second name also gives
            # away a file that only inherited the removed record's inode number.
            opened = os.fstat(fd)
            if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != identity or opened.st_nlink != 1:
                return False
            write_all(fd, data)
        finally:
            os.close(fd)
        return True

    @staticmethod
    def unlink(path: str) -> None:
        try:
            with _relative(path) as (name, dir_fd):
                os.unlink(name, dir_fd=dir_fd)
        except OSError as error:
            # Windows refuses to remove a file another process holds open; the record then outlives its owner until
            # that process exits, which only delays the cleanup a later participant repeats.
            if error.errno not in _MISSING_ERRNOS and not isinstance(error, PermissionError):
                raise

    @staticmethod
    def listdir(path: str) -> list[str]:
        try:
            with _directory(path) as dir_fd, os.scandir(path if dir_fd is None else dir_fd) as entries:
                return [entry.name for entry in entries]
        except FileNotFoundError:
            return []

    def prepare(self, root: str, *, force: bool = False) -> None:
        if self._prepared and not force:
            return
        ensure_directory_exists(root)
        for directory, depth in (
            (Path(root), 0),
            (Path(root, _GENERATIONS_DIRECTORY), 1),
            (Path(root, _HOLDERS_DIRECTORY), 1),
        ):
            with _relative(str(directory), depth) as (name, dir_fd):
                with suppress(FileExistsError):
                    os.mkdir(name, 0o700, dir_fd=dir_fd)
                # mkdir has no O_NOFOLLOW, so check what the name resolves to before anything is created inside it.
                found = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
                parent = directory.parent.stat() if dir_fd is None else os.fstat(dir_fd)
            if not stat.S_ISDIR(found.st_mode):
                msg = f"{directory} exists but is not a directory or is a symlink; refusing to use it"
                raise RuntimeError(msg)
            owner = foreign_directory_owner(found, parent)
            if owner is not None:  # pragma: needs foreign-sticky-tmp
                msg = f"{directory} belongs to uid {owner}, not to this process's account or the parent's owner"
                raise RuntimeError(msg)
        self._prepared = True


@contextmanager
def _relative(path: str, depth: int = 2) -> Generator[tuple[str, int | None]]:
    directory, name = os.path.split(path)
    with _directory(directory, depth) as dir_fd:
        yield (path if dir_fd is None else name), dir_fd


@contextmanager
def _directory(path: str, depth: int = 2) -> Generator[int | None]:
    # O_NOFOLLOW guards only the last component of a path, and what prepare() checked at <root> and <root>/gen holds
    # only until whoever can write the lock's directory renames the tree aside and puts one whose gen is a symlink in
    # its place. So every call opens the root and then the record's directory without following a symlink, and acts
    # inside what it opened. Nothing is kept between calls, so a log removed and started again is found by name as
    # before. None where descriptors cannot anchor a call, and the path is then used whole.
    if depth and _SUPPORTS_DIR_FD:  # pragma: needs link-dir-fd
        with _relative(path, depth - 1) as (name, parent_fd):
            try:
                fd = os.open(name, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | _O_NOFOLLOW, dir_fd=parent_fd)
            except OSError as error:
                if error.errno not in {ELOOP, ENOTDIR}:
                    raise
                msg = f"{path} is not a directory or is a symlink; refusing to use it"
                raise RuntimeError(msg) from error
        try:
            yield fd
        finally:
            os.close(fd)
    else:
        yield None


def _open(path: str, flags: int) -> int | None:
    # Missing is the one answer a caller acts on without raising. A refused open is real on POSIX, where nothing this
    # lock does makes its own records unreadable; on Windows it is usually another process mid-delete or holding the
    # file without sharing, so it is retried through a short grace before it counts, and a file still refusing after
    # that has been in delete-pending for longer than any deletion takes, so it is on its way out: missing.
    deadline: float | None = None
    while True:
        if isinstance(opened := _attempt_open(path, flags), int):
            return opened
        if opened.errno in _MISSING_ERRNOS:
            return None
        if not isinstance(opened, PermissionError) or sys.platform != "win32":
            raise opened
        if deadline is None:  # pragma: win32 cover
            deadline = time.monotonic() + _WINDOWS_OPEN_GRACE
        elif time.monotonic() >= deadline:  # pragma: win32 cover
            return None
        time.sleep(0.002)  # pragma: win32 cover


def _attempt_open(path: str, flags: int) -> int | OSError:
    try:
        with _relative(path) as (name, dir_fd):
            return os.open(name, flags, dir_fd=dir_fd)
    except OSError as error:
        return error


def _identity(path: str, dir_fd: int | None) -> tuple[int, int] | None:
    try:
        st = os.stat(path, dir_fd=dir_fd, follow_symlinks=False)
    except OSError:
        return None
    return st.st_dev, st.st_ino


__all__ = [
    "OsFiles",
]
