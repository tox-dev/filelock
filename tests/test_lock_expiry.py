from __future__ import annotations

import os
import time
from typing import TYPE_CHECKING

import pytest

from filelock import FileLock, SoftFileLock

if TYPE_CHECKING:
    from pathlib import Path

    from pytest_mock import MockerFixture


@pytest.mark.parametrize("lock_type", [FileLock, SoftFileLock])
def test_expired_lock_is_broken(lock_type: type[FileLock | SoftFileLock], tmp_path: Path) -> None:
    lock_path = tmp_path / "test.lock"
    lock_path.touch()
    os.utime(lock_path, (0, 0))

    lock = lock_type(lock_path, lifetime=0.1, timeout=1)
    with lock:
        assert lock.is_locked


def test_soft_non_expired_lock_not_broken(tmp_path: Path) -> None:
    lock_path = tmp_path / "test.lock"
    lock_path.touch()

    lock = SoftFileLock(lock_path, lifetime=9999, timeout=0.2)
    with pytest.raises(TimeoutError):
        lock.acquire()


def test_soft_lifetime_none_no_expiry(tmp_path: Path) -> None:
    lock_path = tmp_path / "test.lock"
    lock_path.touch()
    os.utime(lock_path, (0, 0))

    lock = SoftFileLock(lock_path, lifetime=None, timeout=0.2)
    with pytest.raises(TimeoutError):
        lock.acquire()


def test_expired_lock_race_rename_fails(tmp_path: Path, mocker: MockerFixture) -> None:
    lock_path = tmp_path / "test.lock"
    lock_path.touch()
    os.utime(lock_path, (0, 0))

    mocker.patch("filelock._api.pathlib.Path.rename", side_effect=FileNotFoundError)

    lock = SoftFileLock(lock_path, lifetime=0.1, timeout=0.5)
    with pytest.raises(TimeoutError):
        lock.acquire()


def test_lifetime_property_getter_setter(tmp_path: Path) -> None:
    lock = FileLock(tmp_path / "test.lock", lifetime=10.0)
    assert lock.lifetime == 10.0

    lock.lifetime = 20.0
    assert lock.lifetime == 20.0

    lock.lifetime = None
    assert lock.lifetime is None


def test_lifetime_default_none(tmp_path: Path) -> None:
    lock = FileLock(tmp_path / "test.lock")
    assert lock.lifetime is None


def test_lifetime_singleton_mismatch(tmp_path: Path) -> None:
    lock_path = tmp_path / "test.lock"
    lock1 = FileLock(lock_path, is_singleton=True, lifetime=10.0)
    assert lock1.lifetime == 10.0

    with pytest.raises(ValueError, match="lifetime"):
        FileLock(lock_path, is_singleton=True, lifetime=20.0)


def test_lifetime_singleton_match(tmp_path: Path) -> None:
    lock_path = tmp_path / "test.lock"
    lock1 = FileLock(lock_path, is_singleton=True, lifetime=10.0)
    lock2 = FileLock(lock_path, is_singleton=True, lifetime=10.0)
    assert lock1 is lock2


@pytest.mark.parametrize("lock_type", [FileLock, SoftFileLock])
def test_lock_file_missing_during_expiry_check(lock_type: type[FileLock | SoftFileLock], tmp_path: Path) -> None:
    lock_path = tmp_path / "test.lock"

    lock = lock_type(lock_path, lifetime=0.1, timeout=1)
    with lock:
        assert lock.is_locked


@pytest.mark.parametrize("lock_type", [FileLock, SoftFileLock])
def test_expired_lock_becomes_acquirable(lock_type: type[FileLock | SoftFileLock], tmp_path: Path) -> None:
    lock_path = tmp_path / "test.lock"
    lock_path.touch()
    os.utime(lock_path, (0, 0))

    lock = lock_type(lock_path, lifetime=0.5, timeout=1)
    with lock:
        assert lock.is_locked
    assert not lock.is_locked


@pytest.mark.asyncio
async def test_async_expired_lock_is_broken(tmp_path: Path) -> None:
    from filelock import AsyncFileLock

    lock_path = tmp_path / "test.lock"
    lock_path.touch()
    os.utime(lock_path, (0, 0))

    lock = AsyncFileLock(lock_path, lifetime=0.1, timeout=1)
    async with lock:
        assert lock.is_locked


@pytest.mark.asyncio
async def test_async_soft_non_expired_lock_not_broken(tmp_path: Path) -> None:
    from filelock import AsyncSoftFileLock

    lock_path = tmp_path / "test.lock"
    lock_path.touch()

    lock = AsyncSoftFileLock(lock_path, lifetime=9999, timeout=0.2)
    with pytest.raises(TimeoutError):
        await lock.acquire()


@pytest.mark.parametrize("lock_type", [FileLock, SoftFileLock])
def test_lock_mtime_updated_on_acquire(lock_type: type[FileLock | SoftFileLock], tmp_path: Path) -> None:
    lock_path = tmp_path / "test.lock"
    before = time.time()
    lock = lock_type(lock_path, lifetime=60)
    with lock:
        if lock_path.exists():
            assert lock_path.stat().st_mtime >= before - 1
