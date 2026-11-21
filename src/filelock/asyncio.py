"""Async file lock classes and context managers."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    assert __spec__ is not None

__lazy_modules__ = [f"{__spec__.parent}._async_lock"]

# Explicit imports also support the context classes and metaclass omitted from __all__.
from ._async_lock import (  # ruff:ignore[unused-import]
    AsyncAcquireReturnProxy,
    AsyncFileLockContext,
    AsyncFileLockMeta,
    AsyncSoftFileLease,
    AsyncSoftFileLock,
    AsyncStrictSoftFileLock,
    AsyncThreadLocalFileContext,
    AsyncUnixFileLock,
    AsyncWindowsFileLock,
    BaseAsyncFileLock,
)


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
