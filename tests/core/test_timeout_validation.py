from __future__ import annotations

import math
import time
from decimal import Decimal
from fractions import Fraction
from typing import TYPE_CHECKING, Final

import pytest

from filelock import (
    BaseFileLock,
    FileLock,
    SoftFileLock,
    Timeout,
)

if TYPE_CHECKING:
    from pathlib import Path

    from pytest_mock import MockerFixture


_NANS: Final[list[pytest.ParameterSet]] = [
    pytest.param(math.nan, id="nan"),
    pytest.param(Decimal("sNaN"), id="signaling-nan"),
]


@pytest.mark.parametrize("lock_type", [FileLock, SoftFileLock])
@pytest.mark.parametrize("nan", _NANS)
def test_blocking_acquire_rejects_nan_constructor_timeout(
    lock_type: type[BaseFileLock], nan: float, tmp_path: Path
) -> None:
    lock: Final[BaseFileLock] = lock_type(tmp_path / "a", timeout=nan)
    with pytest.raises(ValueError, match="not nan"):
        lock.acquire(timeout=None)
    assert not lock.is_locked


@pytest.mark.parametrize("lock_type", [FileLock, SoftFileLock])
@pytest.mark.parametrize("nan", _NANS)
def test_blocking_acquire_rejects_nan_argument(lock_type: type[BaseFileLock], nan: float, tmp_path: Path) -> None:
    lock: Final[BaseFileLock] = lock_type(tmp_path / "a", timeout=-1)
    with pytest.raises(ValueError, match="not nan"):
        lock.acquire(timeout=nan)
    assert not lock.is_locked


@pytest.mark.parametrize(
    "timeout",
    [
        pytest.param("5", id="str"),
        pytest.param(None, id="none"),
        pytest.param([1], id="list"),
        pytest.param(True, id="bool"),
    ],
)
def test_constructor_rejects_non_number_timeout(timeout: str | list[int] | bool | None, tmp_path: Path) -> None:
    with pytest.raises(TypeError, match="timeout must be a number of seconds"):
        FileLock(tmp_path / "a", timeout=timeout)  # ty: ignore[invalid-argument-type]  # the rejected type is the contract


@pytest.mark.parametrize(
    "blocking", [pytest.param("no", id="str"), pytest.param(0, id="int"), pytest.param(None, id="none")]
)
def test_constructor_rejects_non_bool_blocking(blocking: str | int | None, tmp_path: Path) -> None:
    with pytest.raises(TypeError, match="blocking must be a bool"):
        FileLock(tmp_path / "a", blocking=blocking)  # ty: ignore[invalid-argument-type]  # the rejected type is the contract


@pytest.mark.parametrize("timeout", [pytest.param(None, id="none"), pytest.param(True, id="bool")])
def test_timeout_setter_rejects_non_number(timeout: bool | None, tmp_path: Path) -> None:
    lock: Final[SoftFileLock] = FileLock(tmp_path / "a", timeout=3)
    with pytest.raises(TypeError, match="timeout must be a number of seconds"):
        lock.timeout = timeout  # ty: ignore[invalid-assignment]  # the rejected type is the contract
    assert lock.timeout == 3


@pytest.mark.parametrize("timeout", [pytest.param("2.5", id="str"), pytest.param(b"2.5", id="bytes")])
def test_timeout_setter_still_takes_a_numeric_string(timeout: str | bytes, tmp_path: Path) -> None:
    lock: Final[SoftFileLock] = FileLock(tmp_path / "a")
    with pytest.deprecated_call(match="setting timeout from a string is deprecated"):
        lock.timeout = timeout
    assert (lock.timeout, type(lock.timeout)) == (2.5, float)


def test_non_blocking_acquire_still_rejects_a_bool_timeout(tmp_path: Path) -> None:
    with pytest.raises(TypeError, match="timeout must be a number of seconds, not bool"):
        FileLock(tmp_path / "a").acquire(timeout=True, blocking=False)


@pytest.mark.parametrize(
    "timeout", [pytest.param(Decimal("0.25"), id="decimal"), pytest.param(Fraction(1, 4), id="fraction")]
)
def test_timeout_takes_any_real_number(timeout: float, tmp_path: Path) -> None:
    lock: Final[SoftFileLock] = FileLock(tmp_path / "a", timeout=timeout)
    assert (type(lock.timeout), lock.timeout) == (float, 0.25)


@pytest.mark.parametrize(
    "timeout",
    [pytest.param(Decimal("-1E-400"), id="decimal"), pytest.param(Fraction(-1, 10**400), id="fraction")],
)
def test_timeout_too_small_for_a_float_stays_negative(timeout: float, tmp_path: Path) -> None:
    # A negative timeout waits without a limit, which rounding to -0.0 would turn into a single attempt.
    assert FileLock(tmp_path / "a", timeout=timeout).timeout < 0


def test_timeout_too_large_for_a_float_raises_value_error(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="timeout is too large to be a number of seconds"):
        FileLock(tmp_path / "a", timeout=10**400)


def test_timeout_setter_stores_a_float(tmp_path: Path) -> None:
    lock: Final[SoftFileLock] = FileLock(tmp_path / "a")
    lock.timeout = 2
    assert (lock.timeout, type(lock.timeout)) == (2.0, float)


def test_blocking_setter_rejects_non_bool(tmp_path: Path) -> None:
    lock: Final[SoftFileLock] = FileLock(tmp_path / "a")
    with pytest.raises(TypeError, match="blocking must be a bool"):
        lock.blocking = "no"  # ty: ignore[invalid-assignment]  # the rejected type is the contract
    assert lock.blocking is True


@pytest.mark.parametrize("blocking", [pytest.param("false", id="str"), pytest.param(0, id="int")])
@pytest.mark.parametrize("lock_type", [FileLock, SoftFileLock])
def test_acquire_rejects_non_bool_blocking(lock_type: type[BaseFileLock], blocking: str | int, tmp_path: Path) -> None:
    lock: Final[BaseFileLock] = lock_type(tmp_path / "a")
    with pytest.raises(TypeError, match="blocking must be a bool"):
        lock.acquire(blocking=blocking)  # ty: ignore[invalid-argument-type]  # the rejected type is the contract
    assert (lock.lock_counter, lock.is_locked) == (0, False)


@pytest.mark.parametrize("lock_type", [FileLock, SoftFileLock])
@pytest.mark.parametrize("nan", _NANS)
def test_non_blocking_acquire_ignores_nan_timeout(lock_type: type[BaseFileLock], nan: float, tmp_path: Path) -> None:
    lock: Final[BaseFileLock] = lock_type(tmp_path / "a")
    with lock.acquire(timeout=nan, blocking=False):
        assert lock.is_locked


@pytest.mark.parametrize("lock_type", [FileLock, SoftFileLock])
def test_poll_wait_stays_within_timeout(tmp_path: Path, lock_type: type[BaseFileLock]) -> None:
    lock_path: Final[Path] = tmp_path / "test.lock"
    with lock_type(lock_path):
        start: Final[float] = time.perf_counter()
        with pytest.raises(Timeout):
            lock_type(lock_path).acquire(timeout=0.05, poll_interval=10)
        assert time.perf_counter() - start < 5


@pytest.mark.parametrize("lock_type", [FileLock, SoftFileLock])
def test_timeout_at_exact_deadline_with_virtual_clock(
    tmp_path: Path, lock_type: type[BaseFileLock], mocker: MockerFixture
) -> None:
    elapsed = 0.0

    def advance_clock(delay: float) -> None:
        nonlocal elapsed
        assert delay > 0
        elapsed += delay

    lock_path = tmp_path / "test.lock"
    with lock_type(lock_path):
        clock = mocker.create_autospec(time, perf_counter=lambda: elapsed, sleep=advance_clock)
        mocker.patch("filelock._core._state.time", new=clock)
        mocker.patch("filelock._core._acquire.time", new=clock)
        with pytest.raises(Timeout):
            lock_type(lock_path).acquire(timeout=1, poll_interval=0.25)
    assert elapsed == 1
