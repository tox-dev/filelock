from __future__ import annotations

import os
import socket
import time
from typing import TYPE_CHECKING, Final

import pytest

from filelock import AsyncFileLock, AsyncSoftFileLock, FileLock, SoftFileLock, Timeout

if TYPE_CHECKING:
    from pathlib import Path

    from pytest_mock import MockerFixture

_NATIVE_IGNORES: Final[str] = "lifetime is ignored"
pytestmark: Final[pytest.MarkDecorator] = pytest.mark.filterwarnings("ignore::filelock.SoftFileLockLifetimeWarning")


def test_expired_soft_lock_is_broken(tmp_path: Path) -> None:
    lock_path = tmp_path / "test.lock"
    lock_path.touch()
    os.utime(lock_path, (0, 0))

    lock = SoftFileLock(lock_path, lifetime=0.1, timeout=1)
    with lock:
        assert lock.is_locked


@pytest.mark.parametrize("lock_type", [FileLock, AsyncFileLock])
def test_native_lifetime_warns_and_is_ignored(lock_type: type[FileLock | AsyncFileLock], tmp_path: Path) -> None:
    with pytest.warns(UserWarning, match=_NATIVE_IGNORES):
        lock = lock_type(tmp_path / "test.lock", lifetime=10.0)
    assert lock.lifetime is None


def test_native_lifetime_setter_warns_and_is_ignored(tmp_path: Path) -> None:
    lock = FileLock(tmp_path / "test.lock")
    with pytest.warns(UserWarning, match=_NATIVE_IGNORES):
        lock.lifetime = 10.0
    assert lock.lifetime is None


def test_native_lifetime_does_not_break_live_holder(tmp_path: Path) -> None:
    lock_path = tmp_path / "test.lock"
    with FileLock(lock_path, timeout=0):
        os.utime(lock_path, (0, 0))  # a broken age-based break would evict this live holder
        with pytest.warns(UserWarning, match=_NATIVE_IGNORES):
            contender = FileLock(lock_path, lifetime=0.1, timeout=0.3)
        with pytest.raises(Timeout):
            contender.acquire()


def test_soft_non_expired_lock_not_broken(tmp_path: Path) -> None:
    lock_path = tmp_path / "test.lock"
    lock_path.write_text(f"{os.getpid()}\n{socket.gethostname()}\n", encoding="utf-8")

    with pytest.raises(TimeoutError):
        SoftFileLock(lock_path, lifetime=9999, timeout=0.2).acquire()


def test_soft_lifetime_none_no_expiry(tmp_path: Path) -> None:
    lock_path = tmp_path / "test.lock"
    lock_path.write_text(f"{os.getpid()}\n{socket.gethostname()}\n", encoding="utf-8")
    os.utime(lock_path, (0, 0))

    with pytest.raises(TimeoutError):
        SoftFileLock(lock_path, lifetime=None, timeout=0.2).acquire()


def test_expired_lock_race_rename_fails(tmp_path: Path, mocker: MockerFixture) -> None:
    lock_path = tmp_path / "test.lock"
    lock_path.touch()
    os.utime(lock_path, (0, 0))

    mocker.patch("filelock._util.Path.rename", side_effect=FileNotFoundError)

    with pytest.raises(TimeoutError):
        SoftFileLock(lock_path, lifetime=0.1, timeout=0.5).acquire()


def test_lifetime_property_getter_setter(tmp_path: Path) -> None:
    lock = SoftFileLock(tmp_path / "test.lock", lifetime=10.0)
    assert lock.lifetime == pytest.approx(10.0)

    lock.lifetime = 20.0
    assert lock.lifetime == pytest.approx(20.0)

    lock.lifetime = None
    assert lock.lifetime is None


def test_lifetime_default_none(tmp_path: Path) -> None:
    assert FileLock(tmp_path / "test.lock").lifetime is None


@pytest.mark.parametrize("bad_value", [-1, -0.5, -1e9])
def test_lifetime_setter_rejects_negative_number(bad_value: float, tmp_path: Path) -> None:
    lock = FileLock(tmp_path / "test.lock")
    with pytest.raises(ValueError, match="non-negative"):
        lock.lifetime = bad_value


@pytest.mark.parametrize("bad_value", ["5", b"5", [1], {1: 2}, complex(1, 0)])
def test_lifetime_setter_rejects_non_numeric(
    bad_value: str | bytes | list[int] | dict[int, int] | complex, tmp_path: Path
) -> None:
    lock = FileLock(tmp_path / "test.lock")
    with pytest.raises(TypeError, match="lifetime must be"):
        lock.lifetime = bad_value  # ty: ignore[invalid-assignment]  # non-numeric input to hit the setter's TypeError


@pytest.mark.parametrize("bad_value", [True, False])  # bool is an int subclass; reject it so it can't read as 1s/0s
def test_lifetime_setter_rejects_bool(bad_value: bool, tmp_path: Path) -> None:
    lock = FileLock(tmp_path / "test.lock")
    with pytest.raises(TypeError, match="lifetime must be"):
        lock.lifetime = bad_value


@pytest.mark.parametrize("value", [0, 0.0])
def test_lifetime_setter_accepts_zero(value: float, tmp_path: Path) -> None:
    lock = SoftFileLock(tmp_path / "test.lock")
    lock.lifetime = value
    assert lock.lifetime == 0


def test_lifetime_singleton_mismatch(tmp_path: Path) -> None:
    lock_path = tmp_path / "test.lock"
    lock1 = SoftFileLock(lock_path, is_singleton=True, lifetime=10.0)
    assert lock1.lifetime == pytest.approx(10.0)

    with pytest.raises(ValueError, match="lifetime"):
        SoftFileLock(lock_path, is_singleton=True, lifetime=20.0)


def test_lifetime_singleton_match(tmp_path: Path) -> None:
    lock_path = tmp_path / "test.lock"
    lock1 = SoftFileLock(lock_path, is_singleton=True, lifetime=10.0)
    lock2 = SoftFileLock(lock_path, is_singleton=True, lifetime=10.0)
    assert lock1 is lock2


def test_lock_file_missing_during_expiry_check(tmp_path: Path) -> None:
    lock = SoftFileLock(tmp_path / "test.lock", lifetime=0.1, timeout=1)
    with lock:
        assert lock.is_locked


def test_expired_lock_becomes_acquirable(tmp_path: Path) -> None:
    lock_path = tmp_path / "test.lock"
    lock_path.touch()
    os.utime(lock_path, (0, 0))

    lock = SoftFileLock(lock_path, lifetime=0.5, timeout=1)
    with lock:
        assert lock.is_locked
    assert not lock.is_locked


@pytest.mark.asyncio
async def test_async_expired_lock_is_broken(tmp_path: Path) -> None:
    lock_path = tmp_path / "test.lock"
    lock_path.touch()
    os.utime(lock_path, (0, 0))

    lock = AsyncSoftFileLock(lock_path, lifetime=0.1, timeout=1)
    async with lock:
        assert lock.is_locked


@pytest.mark.asyncio
async def test_async_soft_non_expired_lock_not_broken(tmp_path: Path) -> None:
    lock_path = tmp_path / "test.lock"
    lock_path.touch()

    with pytest.raises(TimeoutError):
        await AsyncSoftFileLock(lock_path, lifetime=9999, timeout=0.2).acquire()


def test_lock_mtime_updated_on_acquire(tmp_path: Path) -> None:
    lock_path = tmp_path / "test.lock"
    before = time.time()
    with SoftFileLock(lock_path, lifetime=60):
        assert lock_path.exists()  # the soft marker exists while held
        assert lock_path.stat().st_mtime >= before - 1
