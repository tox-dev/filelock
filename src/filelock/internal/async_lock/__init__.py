"""Async lock lifecycle, task ownership and backend dispatch."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from importlib.machinery import ModuleSpec

    from ._read_write import AsyncReadWriteBase

__spec__ = cast("ModuleSpec", __spec__)

__lazy_modules__ = [
    "importlib",
    f"{__spec__.parent}._backends",
    f"{__spec__.parent}._lock",
    f"{__spec__.parent}._owners",
    f"{__spec__.parent}._state",
    f"{__spec__.parent}._transitions",
]

from importlib import import_module

from ._backends import (
    AsyncSoftFileLease,
    AsyncSoftFileLock,
    AsyncStrictSoftFileLock,
    AsyncUnixFileLock,
    AsyncWindowsFileLock,
)
from ._lock import AsyncAcquireReturnProxy, AsyncFileLockMeta, BaseAsyncFileLock
from ._owners import TASKS_THREAD_ID, retire_task_holds_at_exit
from ._state import AsyncFileLockContext, AsyncThreadLocalFileContext
from ._transitions import AsyncTransitionGate, AsyncTransitionUnavailableError


def __getattr__(name: str) -> type[AsyncReadWriteBase]:
    # A sync lock can first release during shutdown, too late to import the executor.
    if name == "AsyncReadWriteBase":
        return import_module("._read_write", __name__).AsyncReadWriteBase
    raise AttributeError(name)


__all__ = [
    "TASKS_THREAD_ID",
    "AsyncAcquireReturnProxy",
    "AsyncFileLockContext",
    "AsyncFileLockMeta",
    "AsyncReadWriteBase",
    "AsyncSoftFileLease",
    "AsyncSoftFileLock",
    "AsyncStrictSoftFileLock",
    "AsyncThreadLocalFileContext",
    "AsyncTransitionGate",
    "AsyncTransitionUnavailableError",
    "AsyncUnixFileLock",
    "AsyncWindowsFileLock",
    "BaseAsyncFileLock",
    "retire_task_holds_at_exit",
]
