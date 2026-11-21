"""Async file lock classes and context managers."""

from __future__ import annotations

__lazy_modules__ = ["filelock.internal.async_lock"]

from filelock.internal import async_lock
from filelock.internal.async_lock import (
    AsyncAcquireReturnProxy,
    AsyncSoftFileLease,
    AsyncSoftFileLock,
    AsyncStrictSoftFileLock,
    AsyncUnixFileLock,
    AsyncWindowsFileLock,
    BaseAsyncFileLock,
)

AsyncFileLockMeta = async_lock.AsyncFileLockMeta
AsyncFileLockContext = async_lock.AsyncFileLockContext
AsyncThreadLocalFileContext = async_lock.AsyncThreadLocalFileContext


def __dir__() -> list[str]:
    # Hide the sync bases and helpers imported at top level.
    return list(__all__)


__all__ = [
    "AsyncAcquireReturnProxy",
    "AsyncSoftFileLease",
    "AsyncSoftFileLock",
    "AsyncStrictSoftFileLock",
    "AsyncUnixFileLock",
    "AsyncWindowsFileLock",
    "BaseAsyncFileLock",
]
