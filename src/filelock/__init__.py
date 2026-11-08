"""
A platform independent file lock that supports the with-statement.

.. autodata:: filelock.__version__
    :no-value:

.. autodata:: filelock.OwnerMode

.. autodata:: filelock.CompromiseReason

"""

from __future__ import annotations

__lazy_modules__ = ["filelock._descriptor", "filelock._error", "importlib"]

import sys  # simplint: ignore[SMP407]  # lazy loader and platform selection live here
import warnings  # simplint: ignore[SMP407]  # lazy loader and platform selection live here
from importlib import import_module  # simplint: ignore[SMP407]  # PEP 562 loader imports submodules by name
from typing import TYPE_CHECKING, Final  # simplint: ignore[SMP407]  # TYPE_CHECKING and Final for module constants

from filelock._api import AcquireReturnProxy, BaseFileLock, CloseErrorPolicy, ContextErrorPolicy, LockOptions
from filelock._descriptor import lock_descriptor, unlock_descriptor
from filelock._error import LeaseSettingsMismatch, SoftFileLockLifetimeWarning, SoftFileLockProtocolError, Timeout
from filelock._unix import UnixFileLock
from filelock._unix import has_fcntl as _has_fcntl  # simplint: ignore[SMP407]  # private probe feeding public has_fcntl
from filelock._windows import WindowsFileLock

# Keeps ``filelock.version`` the string 4.0.12 exposed rather than the submodule.
from filelock.version import version  # simplint: ignore[SMP407]  # API exposes filelock.version as this string

if TYPE_CHECKING:  # simplint: ignore[SMP407]  # type-checker platform lock aliases
    from filelock import asyncio
    from filelock._async_read_write import AsyncAcquireReadWriteReturnProxy, AsyncReadWriteLock
    from filelock._lease import CompromiseReason, LeaseCompromise, SoftFileLease
    from filelock._marker import MarkerSoftFileLock, OwnerMode, OwnerRecord
    from filelock._read_write import ReadWriteLock
    from filelock._soft import SoftFileLock
    from filelock._soft_rw._async import AsyncAcquireSoftReadWriteReturnProxy, AsyncSoftReadWriteLock
    from filelock._soft_rw._sync import SoftReadWriteLock
    from filelock._strict import StrictSoftFileClaim, StrictSoftFileClaimState, StrictSoftFileLock
    from filelock.asyncio import (
        AsyncAcquireReturnProxy,
        AsyncSoftFileLease,
        AsyncSoftFileLock,
        AsyncStrictSoftFileLock,
        AsyncUnixFileLock,
        AsyncWindowsFileLock,
        BaseAsyncFileLock,
    )

#: version of the project as a string
__version__: Final[str] = version  # simplint: ignore[SMP407]  # public __version__ is a Final assignment

#: ``True`` when :func:`fcntl.flock` is available. Off Windows, :data:`FileLock` then resolves to
#: :class:`UnixFileLock`, otherwise to :class:`SoftFileLock`. Always ``False`` on Windows.
#:
#: :meta hide-value:
has_fcntl: Final[bool] = _has_fcntl  # simplint: ignore[SMP102, SMP407]  # public name; renaming breaks importers


if sys.platform == "win32":  # pragma: win32 cover  # simplint: ignore[SMP407]  # platform selection runs at import
    _FileLock: type[BaseFileLock] = WindowsFileLock
else:  # pragma: win32 no cover # ruff:ignore[collapsible-else-if]  # the else carries the win32 no-cover pragma
    if has_fcntl:
        _FileLock: type[BaseFileLock] = UnixFileLock
    else:
        from filelock._soft import SoftFileLock

        _FileLock = SoftFileLock
        warnings.warn("only soft file lock is available", stacklevel=2)

if TYPE_CHECKING:  # simplint: ignore[SMP407]  # type-checker FileLock alias
    #: Alias for the lock, which should be used for the current platform.
    FileLock = SoftFileLock
    #: Alias for the async lock, which should be used for the current platform.
    AsyncFileLock = AsyncSoftFileLock
else:
    #: Alias for the lock, which should be used for the current platform.
    FileLock = _FileLock

# Importing asyncio and sqlite3 costs more than the rest of the package, so names that need them load on first access.
_LAZY_ATTRIBUTES: Final[dict[str, str]] = {  # simplint: ignore[SMP407]  # lookup read by PEP 562 __getattr__
    "AsyncAcquireReadWriteReturnProxy": "._async_read_write",
    "AsyncAcquireReturnProxy": ".asyncio",
    "AsyncAcquireSoftReadWriteReturnProxy": "._soft_rw._async",
    "AsyncFileLock": ".asyncio",
    "AsyncReadWriteLock": "._async_read_write",
    "AsyncSoftFileLease": ".asyncio",
    "AsyncSoftFileLock": ".asyncio",
    "AsyncSoftReadWriteLock": "._soft_rw._async",
    "AsyncStrictSoftFileLock": ".asyncio",
    "AsyncUnixFileLock": ".asyncio",
    "AsyncWindowsFileLock": ".asyncio",
    "BaseAsyncFileLock": ".asyncio",
    "CompromiseReason": "._lease",
    "LeaseCompromise": "._lease",
    "MarkerSoftFileLock": "._marker",
    "OwnerMode": "._marker",
    "OwnerRecord": "._marker",
    "ReadWriteLock": "._read_write",
    "SoftFileLease": "._lease",
    "SoftFileLock": "._soft",
    "SoftReadWriteLock": "._soft_rw._sync",
    "StrictSoftFileClaim": "._strict",
    "StrictSoftFileClaimState": "._strict",
    "StrictSoftFileLock": "._strict",
    "asyncio": ".asyncio",
}


def __getattr__(  # simplint: ignore[SMP407]  # PEP 562 __getattr__ lives here
    name: str,
) -> object:  # simplint: ignore[SMP602]  # lazy attributes span several types
    if (module_name := _LAZY_ATTRIBUTES.get(name)) is None:
        msg = f"module {__name__!r} has no attribute {name!r}"
        raise AttributeError(msg)
    try:
        module = import_module(module_name, __name__)
    except ImportError as error:
        # Only a missing sqlite3 module (the package, CPython's _sqlite3, PyPy's _sqlite3_cffi) leaves the read-write
        # locks None; a frozen application that left out a filelock module gets the error that names it.
        sqlite_missing = (error.name or "").startswith(("sqlite3", "_sqlite3"))
        if module_name not in {"._read_write", "._async_read_write"} or not sqlite_missing:
            raise
        value = None  # pragma: lacks sqlite3
    else:
        if name == "asyncio":
            value = module
        else:
            value = getattr(module, f"Async{_FileLock.__name__}" if name == "AsyncFileLock" else name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:  # simplint: ignore[SMP407]  # PEP 562 __dir__ lives here
    return sorted({*globals(), *_LAZY_ATTRIBUTES})


__all__ = [
    "AcquireReturnProxy",
    "AsyncAcquireReadWriteReturnProxy",
    "AsyncAcquireReturnProxy",
    "AsyncAcquireSoftReadWriteReturnProxy",
    "AsyncFileLock",
    "AsyncReadWriteLock",
    "AsyncSoftFileLease",
    "AsyncSoftFileLock",
    "AsyncSoftReadWriteLock",
    "AsyncStrictSoftFileLock",
    "AsyncUnixFileLock",
    "AsyncWindowsFileLock",
    "BaseAsyncFileLock",
    "BaseFileLock",
    "CloseErrorPolicy",
    "CompromiseReason",
    "ContextErrorPolicy",
    "FileLock",
    "LeaseCompromise",
    "LeaseSettingsMismatch",
    "LockOptions",
    "MarkerSoftFileLock",
    "OwnerMode",
    "OwnerRecord",
    "ReadWriteLock",
    "SoftFileLease",
    "SoftFileLock",
    "SoftFileLockLifetimeWarning",
    "SoftFileLockProtocolError",
    "SoftReadWriteLock",
    "StrictSoftFileClaim",
    "StrictSoftFileClaimState",
    "StrictSoftFileLock",
    "Timeout",
    "UnixFileLock",
    "WindowsFileLock",
    "__version__",
    "asyncio",
    "has_fcntl",
    "lock_descriptor",
    "unlock_descriptor",
]
