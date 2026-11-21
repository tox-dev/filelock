"""Marker locks, leases and their shared record formats."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    assert __spec__ is not None

__lazy_modules__ = ["importlib", f"{__spec__.parent}._records"]

from importlib import import_module
from types import MappingProxyType
from typing import TYPE_CHECKING, Final

from ._records import (
    MALFORMED_LOCK_AGE_THRESHOLD,
    STRICT_SOFT_SENTINEL_RECORD,
    OwnerMode,
    OwnerRecord,
    encode_marker,
    parse_decimal,
    parse_lock_holder,
    parse_marker,
    read_lock_file,
)

if TYPE_CHECKING:
    from ._lease import CompromiseReason, LeaseClaim, LeaseCompromise, SoftFileLease
    from .exclusive import MarkerSoftFileLock, SoftFileLock
    from .read_write import AsyncAcquireSoftReadWriteReturnProxy, AsyncSoftReadWriteLock, SoftReadWriteLock
    from .strict import StrictRelease, StrictSoftFileClaim, StrictSoftFileClaimState, StrictSoftFileLock

_EXPORTS: Final[MappingProxyType[str, str]] = MappingProxyType({
    "SoftFileLock": ".exclusive",
    "MarkerSoftFileLock": ".exclusive",
    "SoftFileLease": "._lease",
    "LeaseClaim": "._lease",
    "LeaseCompromise": "._lease",
    "CompromiseReason": "._lease",
    "AsyncAcquireSoftReadWriteReturnProxy": ".read_write",
    "AsyncSoftReadWriteLock": ".read_write",
    "SoftReadWriteLock": ".read_write",
    "StrictRelease": ".strict",
    "StrictSoftFileClaim": ".strict",
    "StrictSoftFileClaimState": ".strict",
    "StrictSoftFileLock": ".strict",
})


def __getattr__(name: str) -> object:  # simplint: ignore[imprecise-annotation]  # class and type-alias exports
    # Loading an exclusive lock must not import the async reader/writer backend.
    if (module := _EXPORTS.get(name)) is None:
        raise AttributeError(name)
    return getattr(import_module(module, __name__), name)


__all__ = [
    "MALFORMED_LOCK_AGE_THRESHOLD",
    "STRICT_SOFT_SENTINEL_RECORD",
    "AsyncAcquireSoftReadWriteReturnProxy",
    "AsyncSoftReadWriteLock",
    "CompromiseReason",
    "LeaseClaim",
    "LeaseCompromise",
    "MarkerSoftFileLock",
    "OwnerMode",
    "OwnerRecord",
    "SoftFileLease",
    "SoftFileLock",
    "SoftReadWriteLock",
    "StrictRelease",
    "StrictSoftFileClaim",
    "StrictSoftFileClaimState",
    "StrictSoftFileLock",
    "encode_marker",
    "parse_decimal",
    "parse_lock_holder",
    "parse_marker",
    "read_lock_file",
]
