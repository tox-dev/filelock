from __future__ import annotations

from filelock.internal.native import UnixFileLock, WindowsFileLock
from filelock.internal.soft import SoftFileLease, SoftFileLock, StrictSoftFileLock

from ._lock import BaseAsyncFileLock


class AsyncSoftFileLock(SoftFileLock, BaseAsyncFileLock):
    """An asynchronous marker-file lock."""

    __module__ = "filelock.asyncio"

    _lifetime_replacements: tuple[str, str] | None = ("AsyncStrictSoftFileLock", "AsyncSoftFileLease")


class AsyncStrictSoftFileLock(StrictSoftFileLock, BaseAsyncFileLock):
    """An asynchronous lock backed by strict owner claims."""

    __module__ = "filelock.asyncio"

    _timed_release_supported = False


class AsyncSoftFileLease(SoftFileLease, BaseAsyncFileLock):
    """A renewable marker lease that a peer can take after expiry."""

    __module__ = "filelock.asyncio"


class AsyncUnixFileLock(UnixFileLock, BaseAsyncFileLock):
    """An asynchronous Unix lock backed by :func:`fcntl.flock`."""

    __module__ = "filelock.asyncio"


class AsyncWindowsFileLock(WindowsFileLock, BaseAsyncFileLock):
    """An asynchronous Windows byte-range lock."""

    __module__ = "filelock.asyncio"


__all__ = [
    "AsyncSoftFileLease",
    "AsyncSoftFileLock",
    "AsyncStrictSoftFileLock",
    "AsyncUnixFileLock",
    "AsyncWindowsFileLock",
]
