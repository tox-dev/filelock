from __future__ import annotations

from typing import TYPE_CHECKING, Literal, cast

import pytest

from filelock import AsyncFileLock, AsyncSoftFileLock, FileLock, SoftFileLock

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.parametrize(
    "lock_type",
    [
        pytest.param(FileLock, id="native-sync"),
        pytest.param(SoftFileLock, id="soft-sync"),
        pytest.param(AsyncFileLock, id="native-async"),
        pytest.param(AsyncSoftFileLock, id="soft-async"),
    ],
)
@pytest.mark.parametrize(
    "entry_point",
    [pytest.param("constructor", id="constructor"), pytest.param("setter", id="setter")],
)
@pytest.mark.parametrize(
    ("bad_value", "error_type", "message"),
    [
        pytest.param(-1, ValueError, "finite and non-negative", id="negative-int"),
        pytest.param(-0.5, ValueError, "finite and non-negative", id="negative-float"),
        pytest.param(float("nan"), ValueError, "finite and non-negative", id="nan"),
        pytest.param(float("inf"), ValueError, "finite and non-negative", id="positive-infinity"),
        pytest.param(float("-inf"), ValueError, "finite and non-negative", id="negative-infinity"),
        pytest.param(True, TypeError, "lifetime must be", id="true"),
        pytest.param(False, TypeError, "lifetime must be", id="false"),
        pytest.param("5", TypeError, "lifetime must be", id="string"),
        pytest.param(b"5", TypeError, "lifetime must be", id="bytes"),
        pytest.param([1], TypeError, "lifetime must be", id="list"),
        pytest.param({1: 2}, TypeError, "lifetime must be", id="dict"),
        pytest.param(complex(1, 0), TypeError, "lifetime must be", id="complex"),
    ],
)
def test_lifetime_rejects_invalid_value(
    lock_type: type[FileLock | AsyncFileLock],
    entry_point: Literal["constructor", "setter"],
    bad_value: str | bytes | list[int] | dict[int, int] | complex,
    error_type: type[ValueError | TypeError],
    message: str,
    tmp_path: Path,
) -> None:
    lock_path = tmp_path / "test.lock"
    lock = lock_type(lock_path)
    with pytest.raises(error_type, match=message):
        _set_lifetime(lock_type, entry_point, lock, cast("float", bad_value))
    assert (lock.lifetime, lock_path.exists()) == (None, False)


@pytest.mark.parametrize(
    "lock_type",
    [pytest.param(SoftFileLock, id="sync"), pytest.param(AsyncSoftFileLock, id="async")],
)
@pytest.mark.parametrize(
    "entry_point",
    [pytest.param("constructor", id="constructor"), pytest.param("setter", id="setter")],
)
@pytest.mark.parametrize(
    "value",
    [
        pytest.param(None, id="none"),
        pytest.param(0, id="zero-int"),
        pytest.param(0.0, id="zero-float"),
        pytest.param(2.5, id="positive-float"),
        pytest.param(10**1000, id="large-int"),
    ],
)
def test_lifetime_accepts_supported_value(
    lock_type: type[SoftFileLock | AsyncSoftFileLock],
    entry_point: Literal["constructor", "setter"],
    value: float | None,
    tmp_path: Path,
) -> None:
    lock_path = tmp_path / "test.lock"
    if entry_point == "constructor":
        lock = lock_type(lock_path, lifetime=value)
    else:
        lock = lock_type(lock_path)
        lock.lifetime = value
    assert lock.lifetime == value


@pytest.mark.parametrize(
    "bad_value",
    [
        pytest.param(-1, id="negative"),
        pytest.param(float("nan"), id="nan"),
        pytest.param(float("inf"), id="infinity"),
    ],
)
def test_lifetime_singleton_rejection_preserves_instance(bad_value: float, tmp_path: Path) -> None:
    lock_path = tmp_path / "test.lock"
    lock = SoftFileLock(lock_path, is_singleton=True, lifetime=10)

    with pytest.raises(ValueError, match="finite and non-negative"):
        SoftFileLock(lock_path, is_singleton=True, lifetime=bad_value)

    assert SoftFileLock(lock_path, is_singleton=True, lifetime=10) is lock


def _set_lifetime(
    lock_type: type[FileLock | AsyncFileLock],
    entry_point: Literal["constructor", "setter"],
    lock: FileLock | AsyncFileLock,
    value: float,
) -> None:
    if entry_point == "constructor":
        lock_type(lock.lock_file, lifetime=value)
    else:
        lock.lifetime = value
