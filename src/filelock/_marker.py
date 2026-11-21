from __future__ import annotations

__lazy_modules__ = ["filelock.internal.soft"]

from filelock.internal.soft import MarkerSoftFileLock, OwnerRecord

__all__ = [
    "MarkerSoftFileLock",
    "OwnerRecord",
]
