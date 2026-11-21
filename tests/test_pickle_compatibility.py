from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

import filelock
from filelock._api import AcquireReturnProxy, BaseFileLock, LockOptions
from filelock._async_read_write import AsyncAcquireReadWriteReturnProxy, AsyncReadWriteLock
from filelock._descriptor import lock_descriptor, unlock_descriptor
from filelock._error import (
    LeaseSettingsMismatch,
    ReleasePendingError,
    SoftFileLockLifetimeWarning,
    SoftFileLockProtocolError,
    Timeout,
)
from filelock._lease import LeaseCompromise, SoftFileLease
from filelock._marker import MarkerSoftFileLock, OwnerRecord
from filelock._read_write import ReadWriteLock
from filelock._soft import SoftFileLock
from filelock._soft_rw._async import AsyncAcquireSoftReadWriteReturnProxy, AsyncSoftReadWriteLock
from filelock._soft_rw._sync import SoftReadWriteLock
from filelock._strict import StrictSoftFileClaim, StrictSoftFileLock
from filelock._unix import UnixFileLock
from filelock._windows import WindowsFileLock

if TYPE_CHECKING:
    from types import FunctionType


@pytest.mark.parametrize(
    "legacy",
    [
        AcquireReturnProxy,
        BaseFileLock,
        LockOptions,
        AsyncAcquireReadWriteReturnProxy,
        AsyncReadWriteLock,
        lock_descriptor,
        unlock_descriptor,
        LeaseSettingsMismatch,
        ReleasePendingError,
        SoftFileLockLifetimeWarning,
        SoftFileLockProtocolError,
        Timeout,
        LeaseCompromise,
        SoftFileLease,
        MarkerSoftFileLock,
        OwnerRecord,
        ReadWriteLock,
        SoftFileLock,
        AsyncAcquireSoftReadWriteReturnProxy,
        AsyncSoftReadWriteLock,
        SoftReadWriteLock,
        StrictSoftFileClaim,
        StrictSoftFileLock,
        UnixFileLock,
        WindowsFileLock,
    ],
)
def test_pickle_resolves_original_global(legacy: type | FunctionType) -> None:
    assert getattr(filelock, legacy.__name__) is legacy
