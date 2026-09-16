"""The :class:`~._protocol.Files` implementation over :mod:`os`."""

from __future__ import annotations

import os
import stat
from contextlib import suppress
from errno import ENOENT, ESTALE
from pathlib import Path
from typing import Final

from filelock._strict import _link_no_follow, _raise_if_hard_links_unsupported
from filelock._util import write_all

from ._protocol import _GENERATIONS_DIRECTORY, _HOLDERS_DIRECTORY, _MAX_RECORD_SIZE

_O_NOFOLLOW: Final[int] = getattr(os, "O_NOFOLLOW", 0)
#: Keeps an open of a FIFO planted at a record path from blocking; the regular-file check below then rejects it.
_O_NONBLOCK: Final[int] = getattr(os, "O_NONBLOCK", 0)
#: Windows opens descriptors in text mode by default, which rewrites newlines; every record here is exact bytes.
_O_BINARY: Final[int] = getattr(os, "O_BINARY", 0)
#: ESTALE is what an NFS client reports for a file a peer unlinked out from under its cached handle: gone, not broken.
_MISSING_ERRNOS: Final[frozenset[int]] = frozenset({ENOENT, ESTALE})
_OWNER_ONLY_FILE: Final[int] = 0o600
_OWNER_ONLY_DIRECTORY: Final[int] = 0o700


class OsFiles:
    """Record files under a lock's protocol root, opened without following symlinks and owned by this UID alone."""

    def __init__(self, lock_file: str) -> None:
        self._lock_file = lock_file

    @staticmethod
    def read(path: str) -> bytes | None:
        try:
            fd = os.open(path, os.O_RDONLY | _O_NOFOLLOW | _O_NONBLOCK | _O_BINARY)
        except OSError as error:
            # A PermissionError is Windows holding a file in its delete-pending state: on its way out, so missing.
            if error.errno in _MISSING_ERRNOS or isinstance(error, PermissionError):
                return None
            raise
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
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_NOFOLLOW | _O_BINARY, _OWNER_ONLY_FILE)
        try:
            write_all(fd, data)
        except BaseException:
            os.close(fd)
            # The name is unique to this call, so removing it cannot take a record from anyone else.
            cls.unlink(path)
            raise
        os.close(fd)

    def link(self, source: str, target: str) -> bool:
        try:
            _link_no_follow(source, target)
        except FileExistsError:
            pass
        except (NotImplementedError, OSError) as error:
            _raise_if_hard_links_unsupported(self._lock_file, error)
        # Whatever the call reported, the file's identity decides: an NFS retransmit can turn a link that landed into
        # an error, and an EEXIST can be this very link answered twice.
        return (identity := _identity(source)) is not None and identity == _identity(target)

    @staticmethod
    def overwrite(path: str, data: bytes) -> bool:
        try:
            fd = os.open(path, os.O_WRONLY | _O_NOFOLLOW | _O_BINARY)
        except OSError as error:
            if error.errno in _MISSING_ERRNOS or isinstance(error, PermissionError):
                return False
            raise
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
        for directory in (Path(root), Path(root, _GENERATIONS_DIRECTORY), Path(root, _HOLDERS_DIRECTORY)):
            with suppress(FileExistsError):
                directory.mkdir(mode=_OWNER_ONLY_DIRECTORY)
            # mkdir has no O_NOFOLLOW, so check what the name resolves to before anything is created inside it.
            mode = directory.lstat().st_mode
            if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
                msg = f"{directory} exists but is not a directory or is a symlink; refusing to use it"
                raise RuntimeError(msg)


def _identity(path: str) -> tuple[int, int] | None:
    try:
        st = os.lstat(path)
    except OSError:
        return None
    return st.st_dev, st.st_ino


__all__ = [
    "OsFiles",
]
