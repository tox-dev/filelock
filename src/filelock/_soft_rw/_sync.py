from __future__ import annotations

__lazy_modules__ = ["filelock.internal.soft.read_write"]

from filelock.internal.soft.read_write import SoftReadWriteLock

__all__ = [
    "SoftReadWriteLock",
]
