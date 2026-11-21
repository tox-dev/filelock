from __future__ import annotations

import inspect
import os
import sys
from errno import ENOSYS
from typing import TYPE_CHECKING

import pytest

from filelock import (
    BaseFileLock,
    FileLock,
    SoftFileLock,
    Timeout,
    UnixFileLock,
    WindowsFileLock,
)
from tests.capability_marks import (
    NEEDS_FCNTL,
)

if TYPE_CHECKING:
    from pathlib import Path

    from pytest_mock import MockerFixture


@pytest.mark.parametrize(
    "lock_type",
    [
        pytest.param(
            WindowsFileLock,
            id="WindowsFileLock",
            marks=pytest.mark.skipif(sys.platform == "win32", reason="WindowsFileLock is the native lock here"),
        ),
        pytest.param(
            UnixFileLock,
            id="UnixFileLock",
            marks=pytest.mark.skipif(sys.platform != "win32", reason="UnixFileLock is the native lock here"),
        ),
    ],
)
def test_wrong_platform(lock_type: type[BaseFileLock], tmp_path: Path) -> None:
    assert not inspect.isabstract(UnixFileLock)
    assert not inspect.isabstract(WindowsFileLock)
    assert inspect.isabstract(BaseFileLock)

    lock = lock_type(tmp_path / "lockfile")

    with pytest.raises(NotImplementedError):
        lock.acquire()
    with pytest.raises(NotImplementedError):
        lock._release()


@NEEDS_FCNTL  # pragma: needs fcntl
@pytest.mark.filterwarnings("ignore:flock not supported on this filesystem:UserWarning")
def test_flock_not_implemented_unix(tmp_path: Path, mocker: MockerFixture) -> None:
    mocker.patch("fcntl.flock", side_effect=OSError(ENOSYS, "mock error"), autospec=True)
    lock = FileLock(tmp_path / "a.lock")
    with lock:
        assert lock.is_locked
        assert SoftFileLock(tmp_path / "a.lock").is_lock_held_by_us


@NEEDS_FCNTL  # pragma: needs fcntl
def test_fallback_to_soft_disabled_raises_enosys(tmp_path: Path, mocker: MockerFixture) -> None:
    mocker.patch("filelock.internal.native._unix.fcntl.flock", side_effect=OSError(ENOSYS, "no flock"), autospec=True)
    lock = FileLock(str(tmp_path / "a"), fallback_to_soft=False)
    with pytest.raises(OSError, match="no flock") as info:
        lock.acquire()
    assert info.value.errno == ENOSYS
    assert not lock.is_locked
    assert type(lock).__name__ == "UnixFileLock"
    assert lock.lock_counter == 0


@NEEDS_FCNTL  # pragma: needs fcntl
def test_fallback_to_soft_default_switches_to_soft(tmp_path: Path, mocker: MockerFixture) -> None:
    mocker.patch("filelock.internal.native._unix.fcntl.flock", side_effect=OSError(ENOSYS, "no flock"), autospec=True)
    lock = FileLock(str(tmp_path / "a"))
    with pytest.warns(UserWarning, match="falling back to SoftFileLock"):
        lock.acquire()
    try:
        assert lock.is_locked
        assert SoftFileLock(str(tmp_path / "a")).is_lock_held_by_us
    finally:
        lock.release()


@NEEDS_FCNTL  # pragma: needs fcntl
def test_fallback_disabled_recursive_reraises(tmp_path: Path, mocker: MockerFixture) -> None:
    mocker.patch("filelock.internal.native._unix.fcntl.flock", side_effect=OSError(ENOSYS, "no flock"), autospec=True)
    lock = FileLock(str(tmp_path / "a"), fallback_to_soft=False)
    for _ in range(2):
        with pytest.raises(OSError, match="no flock"):
            lock.acquire()
        assert not lock.is_locked


@NEEDS_FCNTL  # pragma: needs fcntl
def test_singleton_rejects_different_fallback_to_soft(tmp_path: Path) -> None:
    path = str(tmp_path / "a")
    first = FileLock(path, is_singleton=True, fallback_to_soft=True)
    try:
        with pytest.raises(ValueError, match="fallback_to_soft"):
            FileLock(path, is_singleton=True, fallback_to_soft=False)
    finally:
        first.release(force=True)


@NEEDS_FCNTL  # pragma: needs fcntl
def test_unix_acquire_without_o_nofollow(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delattr(os, "O_NOFOLLOW", raising=False)
    lock = FileLock(str(tmp_path / "a"), is_singleton=False)
    lock.acquire()
    try:
        assert lock.is_locked
    finally:
        lock.release()


@NEEDS_FCNTL  # pragma: needs fcntl
def test_unix_soft_fallback_keeps_a_replaced_file(tmp_path: Path, mocker: MockerFixture) -> None:
    lock_path = tmp_path / "a"
    lock = FileLock(str(lock_path), is_singleton=False)
    target = lock.lock_file
    mocker.patch("filelock.internal.native._unix.fcntl.flock", side_effect=OSError(ENOSYS, "no flock"), autospec=True)

    real_lstat = os.lstat

    def lstat_swapped_identity(path: str) -> os.stat_result:
        st = real_lstat(path)
        if str(path) != target:
            return st
        fields = list(st)
        fields[1] += 1  # bump st_ino so the on-disk identity no longer matches the opened fd
        return os.stat_result(fields)

    mocker.patch("filelock.internal.native._unix.os.lstat", side_effect=lstat_swapped_identity, autospec=True)

    with pytest.raises(Timeout), pytest.warns(UserWarning, match="falling back to SoftFileLock"):
        lock.acquire(timeout=0.2)

    assert lock_path.exists()
