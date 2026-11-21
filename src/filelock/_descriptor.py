from __future__ import annotations

__lazy_modules__ = ["filelock.internal.native"]

from filelock.internal.native import lock_descriptor, unlock_descriptor

__all__ = [
    "lock_descriptor",
    "unlock_descriptor",
]
