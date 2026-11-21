from __future__ import annotations

__lazy_modules__ = ["filelock.internal.native"]

from filelock.internal.native import UnixFileLock

__all__ = [
    "UnixFileLock",
]
