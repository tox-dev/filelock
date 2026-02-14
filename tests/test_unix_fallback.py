from __future__ import annotations

import os
import socket
import sys
from errno import ENOSYS
from typing import TYPE_CHECKING

import pytest

from filelock import SoftFileLock, UnixFileLock

if TYPE_CHECKING:
    from pathlib import Path
    from unittest.mock import MagicMock

    from pytest_mock import MockerFixture

unix_only = pytest.mark.skipif(sys.platform == "win32", reason="unix-only flock fallback")
_ENOSYS_SIDE_EFFECT = OSError(ENOSYS, "Function not implemented")
_FLOCK_PATCH_TARGET = "filelock._unix.fcntl.flock"


@unix_only
def test_fallback_emits_warning(tmp_path: Path, mocker: MockerFixture) -> None:
    mocker.patch(_FLOCK_PATCH_TARGET, side_effect=_ENOSYS_SIDE_EFFECT)
    lock = UnixFileLock(tmp_path / "test.lock")

    with pytest.warns(UserWarning, match="flock not supported on this filesystem, falling back to SoftFileLock"):
        lock.acquire()
    lock.release()


@unix_only
@pytest.mark.filterwarnings("default::UserWarning")
def test_fallback_swaps_to_soft(tmp_path: Path, mocker: MockerFixture) -> None:
    mocker.patch(_FLOCK_PATCH_TARGET, side_effect=_ENOSYS_SIDE_EFFECT)
    lock = UnixFileLock(tmp_path / "test.lock")

    with lock:
        assert lock.is_locked
        assert isinstance(lock, SoftFileLock)


@unix_only
@pytest.mark.filterwarnings("default::UserWarning")
def test_fallback_writes_pid_and_hostname(tmp_path: Path, mocker: MockerFixture) -> None:
    lock_path = tmp_path / "test.lock"
    mocker.patch(_FLOCK_PATCH_TARGET, side_effect=_ENOSYS_SIDE_EFFECT)
    lock = UnixFileLock(lock_path)

    with lock:
        content = lock_path.read_text(encoding="utf-8")
        assert content == f"{os.getpid()}\n{socket.gethostname()}\n"


@unix_only
@pytest.mark.filterwarnings("default::UserWarning")
def test_fallback_release_unlinks_file(tmp_path: Path, mocker: MockerFixture) -> None:
    lock_path = tmp_path / "test.lock"
    mocker.patch(_FLOCK_PATCH_TARGET, side_effect=_ENOSYS_SIDE_EFFECT)
    lock = UnixFileLock(lock_path)

    lock.acquire()
    assert lock_path.exists()
    lock.release()
    assert not lock_path.exists()


@unix_only
@pytest.mark.filterwarnings("default::UserWarning")
def test_fallback_subsequent_acquire_skips_flock(tmp_path: Path, mocker: MockerFixture) -> None:
    flock_mock: MagicMock = mocker.patch(_FLOCK_PATCH_TARGET, side_effect=_ENOSYS_SIDE_EFFECT)
    lock = UnixFileLock(tmp_path / "test.lock")

    lock.acquire()
    lock.release()
    flock_mock.reset_mock()

    lock.acquire()
    lock.release()
    flock_mock.assert_not_called()


@unix_only
@pytest.mark.filterwarnings("default::UserWarning")
def test_fallback_reentrant_locking(tmp_path: Path, mocker: MockerFixture) -> None:
    mocker.patch(_FLOCK_PATCH_TARGET, side_effect=_ENOSYS_SIDE_EFFECT)
    lock = UnixFileLock(tmp_path / "test.lock")

    with lock:
        with lock:
            assert lock.is_locked
        assert lock.is_locked
    assert not lock.is_locked
