from __future__ import annotations

__lazy_modules__ = ["filelock.internal.sqlite"]

from filelock.internal.sqlite import ReadWriteLock

__all__ = [
    "ReadWriteLock",
]
