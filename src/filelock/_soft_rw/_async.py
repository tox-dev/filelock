from __future__ import annotations

__lazy_modules__ = ["filelock.internal.soft.read_write"]

from filelock.internal.soft.read_write import AsyncAcquireSoftReadWriteReturnProxy, AsyncSoftReadWriteLock

__all__ = [
    "AsyncAcquireSoftReadWriteReturnProxy",
    "AsyncSoftReadWriteLock",
]
