"""Cross-process and cross-host reader/writer lock over a generation log of immutable snapshots."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast  # simplint: ignore[init-module-content]  # lazy import spec

if TYPE_CHECKING:
    from importlib.machinery import ModuleSpec

__spec__ = cast("ModuleSpec", __spec__)  # simplint: ignore[init-module-content]  # lazy import spec

__lazy_modules__ = [f"{__spec__.parent}._async", f"{__spec__.parent}._sync"]

from ._async import AsyncAcquireSoftReadWriteReturnProxy, AsyncSoftReadWriteLock
from ._sync import SoftReadWriteLock

__all__ = [
    "AsyncAcquireSoftReadWriteReturnProxy",
    "AsyncSoftReadWriteLock",
    "SoftReadWriteLock",
]
