from __future__ import annotations

__lazy_modules__ = ["filelock.internal.soft"]

from filelock.internal.soft import LeaseCompromise, SoftFileLease

__all__ = [
    "LeaseCompromise",
    "SoftFileLease",
]
