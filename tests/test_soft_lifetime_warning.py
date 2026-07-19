from __future__ import annotations

import sys
import warnings
from typing import TYPE_CHECKING, Literal

import pytest

from filelock import (
    AsyncSoftFileLock,
    AsyncUnixFileLock,
    AsyncWindowsFileLock,
    SoftFileLock,
    SoftFileLockLifetimeWarning,
    Timeout,
    UnixFileLock,
    WindowsFileLock,
)

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.parametrize(
    ("lock_type", "strict_lock", "lease"),
    [
        pytest.param(SoftFileLock, "StrictSoftFileLock", "SoftFileLease", id="sync"),
        pytest.param(AsyncSoftFileLock, "AsyncStrictSoftFileLock", "AsyncSoftFileLease", id="async"),
    ],
)
@pytest.mark.parametrize(
    "entry_point",
    [pytest.param("constructor", id="constructor"), pytest.param("setter", id="setter")],
)
def test_soft_lifetime_warning_identifies_caller(
    lock_type: type[SoftFileLock | AsyncSoftFileLock],
    strict_lock: str,
    lease: str,
    entry_point: Literal["constructor", "setter"],
    tmp_path: Path,
) -> None:
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        if entry_point == "constructor":
            warning_line = sys._getframe().f_lineno + 1
            lock_type(tmp_path / "test.lock", lifetime=10)
        else:
            lock = lock_type(tmp_path / "test.lock")
            warning_line = sys._getframe().f_lineno + 1
            lock.lifetime = 10

    assert [(item.category, str(item.message), item.filename, item.lineno) for item in caught] == [
        (
            SoftFileLockLifetimeWarning,
            (
                f"{lock_type.__name__}(lifetime=...) uses age-based expiry and can overlap a live holder; "
                f"use {lease} for expiry or {strict_lock} for fail-closed locking"
            ),
            __file__,
            warning_line,
        )
    ]


@pytest.mark.parametrize(
    "lock_type",
    [pytest.param(SoftFileLock, id="sync"), pytest.param(AsyncSoftFileLock, id="async")],
)
@pytest.mark.parametrize(
    "entry_point",
    [pytest.param("constructor", id="constructor"), pytest.param("setter", id="setter")],
)
def test_soft_lifetime_none_does_not_warn(
    lock_type: type[SoftFileLock | AsyncSoftFileLock], entry_point: Literal["constructor", "setter"], tmp_path: Path
) -> None:
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        if entry_point == "constructor":
            lock_type(tmp_path / "test.lock", lifetime=None)
        else:
            lock = lock_type(tmp_path / "test.lock")
            lock.lifetime = None

    assert caught == []


@pytest.mark.parametrize(
    "lock_type",
    [pytest.param(SoftFileLock, id="sync"), pytest.param(AsyncSoftFileLock, id="async")],
)
@pytest.mark.parametrize(
    "entry_point",
    [pytest.param("constructor", id="constructor"), pytest.param("setter", id="setter")],
)
def test_invalid_soft_lifetime_does_not_warn(
    lock_type: type[SoftFileLock | AsyncSoftFileLock], entry_point: Literal["constructor", "setter"], tmp_path: Path
) -> None:
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        with pytest.raises(ValueError, match="finite and non-negative"):
            _set_invalid_lifetime(lock_type, entry_point, tmp_path / "test.lock")

    assert caught == []


@pytest.mark.parametrize(
    "lock_type",
    [
        pytest.param(UnixFileLock, marks=pytest.mark.skipif(sys.platform == "win32", reason="Unix backend"), id="sync"),
        pytest.param(
            AsyncUnixFileLock,
            marks=pytest.mark.skipif(sys.platform == "win32", reason="Unix backend"),
            id="async",
        ),
        pytest.param(
            WindowsFileLock, marks=pytest.mark.skipif(sys.platform != "win32", reason="Windows backend"), id="sync"
        ),
        pytest.param(
            AsyncWindowsFileLock,
            marks=pytest.mark.skipif(sys.platform != "win32", reason="Windows backend"),
            id="async",
        ),
    ],
)
@pytest.mark.parametrize(
    "entry_point",
    [pytest.param("constructor", id="constructor"), pytest.param("setter", id="setter")],
)
def test_native_lifetime_warning_identifies_caller(
    lock_type: type[UnixFileLock | AsyncUnixFileLock | WindowsFileLock | AsyncWindowsFileLock],
    entry_point: Literal["constructor", "setter"],
    tmp_path: Path,
) -> None:
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        if entry_point == "constructor":
            warning_line = sys._getframe().f_lineno + 1
            lock_type(tmp_path / "test.lock", lifetime=10)
        else:
            lock = lock_type(tmp_path / "test.lock")
            warning_line = sys._getframe().f_lineno + 1
            lock.lifetime = 10

    assert [(item.category, str(item.message), item.filename, item.lineno) for item in caught] == [
        (
            UserWarning,
            (
                f"lifetime is ignored for {lock_type.__name__}: a native OS lock cannot be broken safely by file age; "
                "only SoftFileLock supports lifetime-based expiry"
            ),
            __file__,
            warning_line,
        )
    ]


@pytest.mark.parametrize(
    "lock_type",
    [pytest.param(SoftFileLock, id="sync"), pytest.param(AsyncSoftFileLock, id="async")],
)
def test_soft_lifetime_singleton_match_warns_for_each_configuration(
    lock_type: type[SoftFileLock | AsyncSoftFileLock], tmp_path: Path
) -> None:
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        first = lock_type(tmp_path / "test.lock", lifetime=10, is_singleton=True)
        second = lock_type(tmp_path / "test.lock", lifetime=10, is_singleton=True)

    assert (first is second, len(caught)) == (True, 2)


@pytest.mark.parametrize(
    "lock_type",
    [pytest.param(SoftFileLock, id="sync"), pytest.param(AsyncSoftFileLock, id="async")],
)
def test_soft_lifetime_singleton_mismatch_warns_before_rejection(
    lock_type: type[SoftFileLock | AsyncSoftFileLock], tmp_path: Path
) -> None:
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        first = lock_type(tmp_path / "test.lock", lifetime=10, is_singleton=True)
        with pytest.raises(ValueError, match="lifetime"):
            lock_type(tmp_path / "test.lock", lifetime=20, is_singleton=True)

    assert (first.lifetime, len(caught)) == (10, 2)


def test_soft_lifetime_polling_does_not_repeat_warning(tmp_path: Path) -> None:
    lock_path = tmp_path / "test.lock"
    holder = SoftFileLock(lock_path)
    with pytest.warns(SoftFileLockLifetimeWarning):
        contender = SoftFileLock(lock_path, lifetime=60, poll_interval=0.001, timeout=0.01)

    with holder, warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        with pytest.raises(Timeout):
            contender.acquire()

    assert caught == []


@pytest.mark.asyncio
async def test_async_soft_lifetime_polling_does_not_repeat_warning(tmp_path: Path) -> None:
    lock_path = tmp_path / "test.lock"
    holder = AsyncSoftFileLock(lock_path)
    with pytest.warns(SoftFileLockLifetimeWarning):
        contender = AsyncSoftFileLock(lock_path, lifetime=60, poll_interval=0.001, timeout=0.01)

    async with holder:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            # The holder never releases inside this block, so the acquire always times out and the body never
            # completes normally.
            with pytest.raises(Timeout):  # pragma: no branch
                await contender.acquire()

    assert caught == []


def _set_invalid_lifetime(
    lock_type: type[SoftFileLock | AsyncSoftFileLock],
    entry_point: Literal["constructor", "setter"],
    lock_path: Path,
) -> None:
    if entry_point == "constructor":
        lock_type(lock_path, lifetime=-1)
    else:
        lock = lock_type(lock_path)
        lock.lifetime = -1
