"""The :class:`~._protocol.Files` implementation over :mod:`os`."""

from __future__ import annotations

import os
import stat
import sys
import time
from contextlib import suppress
from errno import ENOENT, ESTALE
from pathlib import Path
from typing import Final

from filelock._strict import _link_no_follow, _raise_if_hard_links_unsupported
from filelock._util import ensure_directory_exists, write_all

from ._protocol import _GENERATIONS_DIRECTORY, _HOLDERS_DIRECTORY, _MAX_RECORD_SIZE

_O_NOFOLLOW: Final[int] = getattr(os, "O_NOFOLLOW", 0)
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
    def create(cls, path: str, data: bytes) -> None:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_NOFOLLOW | _O_BINARY, 0o600)
        try:
            write_all(fd, data)
        except BaseException:
            os.close(fd)
            # The name is unique to this call, so removing it cannot take a record from anyone else.
            cls.unlink(path)
            raise
        os.close(fd)

    def link(self, source: str, target: str) -> bool:
        failure: NotImplementedError | OSError | None = None
        try:
            _link_no_follow(source, target)
        except FileExistsError:
            pass
        except (NotImplementedError, OSError) as error:
            _raise_if_hard_links_unsupported(self._lock_file, error)
            failure = error
        # Whatever the call reported, the file's identity decides: an NFS retransmit can turn a link that landed into
        # an error, and an EEXIST can be this very link answered twice. Any other error on a link that did not land is
        # a real fault, not a peer winning the race, and retrying it would spin.
        if (identity := _identity(source)) is not None and identity == _identity(target):
            return True
        if failure is not None:
            raise failure
        return False

    @staticmethod
    def overwrite(path: str, data: bytes) -> bool:
        if (fd := _open(path, os.O_WRONLY | _O_NOFOLLOW | _O_BINARY)) is None:
            return False
        try:
            write_all(fd, data)
        finally:
            os.close(fd)
        return True

    @staticmethod
    def unlink(path: str) -> None:
        try:
            Path(path).unlink()
        except OSError as error:
            # Windows refuses to remove a file another process holds open; the record then outlives its owner until
            # that process exits, which only delays the cleanup a later participant repeats.
            if error.errno not in _MISSING_ERRNOS and not isinstance(error, PermissionError):
                raise

    @staticmethod
    def listdir(path: str) -> list[str]:
        try:
            return [entry.name for entry in Path(path).iterdir()]
        except FileNotFoundError:
            return []

    @staticmethod
    def prepare(root: str) -> None:
        ensure_directory_exists(root)
        for directory in (Path(root), Path(root, _GENERATIONS_DIRECTORY), Path(root, _HOLDERS_DIRECTORY)):
            with suppress(FileExistsError):
                directory.mkdir(mode=0o700)
            # mkdir has no O_NOFOLLOW, so check what the name resolves to before anything is created inside it.
            mode = directory.lstat().st_mode
            if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
                msg = f"{directory} exists but is not a directory or is a symlink; refusing to use it"
                raise RuntimeError(msg)


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
        return os.open(path, flags)
    except OSError as error:
        return error


def _identity(path: str) -> tuple[int, int] | None:
    try:
        st = os.lstat(path)
    except OSError:
        return None
    return st.st_dev, st.st_ino


__all__ = [
    "OsFiles",
]
