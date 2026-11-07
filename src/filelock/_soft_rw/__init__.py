"""Cross-process and cross-host reader/writer lock over a generation log of immutable snapshots."""

from __future__ import annotations

__lazy_modules__ = [f"{__spec__.parent}._async", f"{__spec__.parent}._sync"]

from ._async import AsyncAcquireSoftReadWriteReturnProxy, AsyncSoftReadWriteLock
from ._sync import SoftReadWriteLock

__all__ = [
    "AsyncAcquireSoftReadWriteReturnProxy",
    "AsyncSoftReadWriteLock",
    "SoftReadWriteLock",
]
