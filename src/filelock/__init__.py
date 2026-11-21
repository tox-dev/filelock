"""
A platform independent file lock that supports the with-statement.

.. autodata:: filelock.__version__
    :no-value:

.. autodata:: filelock.OwnerMode

.. autodata:: filelock.CompromiseReason

"""

from __future__ import annotations

__lazy_modules__ = ["filelock.internal.core", "importlib"]

import sys
import warnings
from importlib import import_module
from typing import TYPE_CHECKING, Final

from filelock.internal.core import (
    AcquireReturnProxy,
    BaseFileLock,
    CloseErrorPolicy,
    ContextErrorPolicy,
    LeaseSettingsMismatch,
    LockOptions,
    ReleasePendingError,
    SoftFileLockLifetimeWarning,
    SoftFileLockProtocolError,
    Timeout,
)
from filelock.internal.native import (
    UnixFileLock,
    WindowsFileLock,
    lock_descriptor,
    unlock_descriptor,
)
from filelock.internal.native import has_fcntl as unix_has_fcntl

# Keeps ``filelock.version`` the string 4.0.12 exposed rather than the submodule.
from filelock.version import version

if TYPE_CHECKING:
    from filelock import asyncio
    from filelock.asyncio import (
        AsyncAcquireReturnProxy,
        AsyncSoftFileLease,
        AsyncSoftFileLock,
        AsyncStrictSoftFileLock,
        AsyncUnixFileLock,
        AsyncWindowsFileLock,
        BaseAsyncFileLock,
    )
    from filelock.internal.soft import (
        AsyncAcquireSoftReadWriteReturnProxy,
        AsyncSoftReadWriteLock,
        CompromiseReason,
        LeaseCompromise,
        MarkerSoftFileLock,
        OwnerMode,
        OwnerRecord,
        SoftFileLease,
        SoftFileLock,
        SoftReadWriteLock,
        StrictSoftFileClaim,
        StrictSoftFileClaimState,
        StrictSoftFileLock,
    )
    from filelock.internal.sqlite import (
        AsyncAcquireReadWriteReturnProxy,
        AsyncReadWriteLock,
        ReadWriteLock,
    )

#: version of the project as a string
__version__: Final[str] = version

#: ``True`` when :func:`fcntl.flock` is available. Off Windows, :data:`FileLock` then resolves to
#: :class:`UnixFileLock`, otherwise to :class:`SoftFileLock`. Always ``False`` on Windows.
#:
#: :meta hide-value:
has_fcntl: Final[bool] = unix_has_fcntl  # simplint: ignore[module-variable-case]  # public API


if sys.platform == "win32":  # pragma: win32 cover
    _FileLock: type[BaseFileLock] = WindowsFileLock
else:  # pragma: win32 no cover # ruff:ignore[collapsible-else-if]  # the else carries the win32 no-cover pragma
    if has_fcntl:
        _FileLock: type[BaseFileLock] = UnixFileLock
    else:
        from filelock.internal.soft import SoftFileLock

        _FileLock = SoftFileLock
        warnings.warn("only soft file lock is available", stacklevel=2)

if TYPE_CHECKING:
    #: Alias for the lock, which should be used for the current platform.
    FileLock = SoftFileLock
    #: Alias for the async lock, which should be used for the current platform.
    AsyncFileLock = AsyncSoftFileLock
else:
    #: Alias for the lock, which should be used for the current platform.
    FileLock = _FileLock

# Importing asyncio and sqlite3 costs more than the rest of the package, so names that need them load on first access.
_LAZY_ATTRIBUTES: Final[dict[str, str]] = {
    "AsyncAcquireReadWriteReturnProxy": ".internal.sqlite",
    "AsyncAcquireReturnProxy": ".asyncio",
    "AsyncAcquireSoftReadWriteReturnProxy": ".internal.soft",
    "AsyncFileLock": ".asyncio",
    "AsyncReadWriteLock": ".internal.sqlite",
    "AsyncSoftFileLease": ".asyncio",
    "AsyncSoftFileLock": ".asyncio",
    "AsyncSoftReadWriteLock": ".internal.soft",
    "AsyncStrictSoftFileLock": ".asyncio",
    "AsyncUnixFileLock": ".asyncio",
    "AsyncWindowsFileLock": ".asyncio",
    "BaseAsyncFileLock": ".asyncio",
    "CompromiseReason": ".internal.soft",
    "LeaseCompromise": ".internal.soft",
    "MarkerSoftFileLock": ".internal.soft",
    "OwnerMode": ".internal.soft",
    "OwnerRecord": ".internal.soft",
    "ReadWriteLock": ".internal.sqlite",
    "SoftFileLease": ".internal.soft",
    "SoftFileLock": ".internal.soft",
    "SoftReadWriteLock": ".internal.soft",
    "StrictSoftFileClaim": ".internal.soft",
    "StrictSoftFileClaimState": ".internal.soft",
    "StrictSoftFileLock": ".internal.soft",
    "asyncio": ".asyncio",
}


def __getattr__(
    name: str,
) -> object:  # simplint: ignore[imprecise-annotation]  # lazy attributes span several types
    if (module_name := _LAZY_ATTRIBUTES.get(name)) is None:
        msg = f"module {__name__!r} has no attribute {name!r}"
        raise AttributeError(msg)
    try:
        module = import_module(module_name, __name__)
        if name == "asyncio":
            value = module
        else:
            value = getattr(module, f"Async{_FileLock.__name__}" if name == "AsyncFileLock" else name)
    except ImportError as error:
        # Only a missing sqlite3 module (the package, CPython's _sqlite3, PyPy's _sqlite3_cffi) leaves the read-write
        # locks None; a frozen application that left out a filelock module gets the error that names it.
        sqlite_missing = (error.name or "").startswith(("sqlite3", "_sqlite3"))
        if module_name != ".internal.sqlite" or not sqlite_missing:
            raise
        value = None  # pragma: lacks sqlite3
    globals()[name] = value
    return value


def __dir__() -> list[str]:
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
    "ReleasePendingError",
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
