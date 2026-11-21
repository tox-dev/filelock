from __future__ import annotations

__lazy_modules__ = ["filelock.internal.core"]

from filelock.internal.core import AcquireReturnProxy, BaseFileLock, LockOptions

__all__ = [
    "AcquireReturnProxy",
    "BaseFileLock",
    "LockOptions",
]
