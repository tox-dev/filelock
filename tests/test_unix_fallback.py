from __future__ import annotations

import os
import socket
import sys
from errno import EIO, ENOSYS
from typing import TYPE_CHECKING, Final

import pytest

from filelock import SoftFileLock, UnixFileLock

if TYPE_CHECKING:
    from pathlib import Path
    from unittest.mock import MagicMock

    from pytest_mock import MockerFixture

_UNIX_ONLY: Final[pytest.MarkDecorator] = pytest.mark.skipif(sys.platform == "win32", reason="unix-only flock fallback")


@_UNIX_ONLY
@pytest.mark.usefixtures("unsupported_flock")
def test_fallback_emits_warning(tmp_path: Path) -> None:
    lock = UnixFileLock(tmp_path / "test.lock")

    with pytest.warns(UserWarning, match="flock not supported on this filesystem, falling back to SoftFileLock"):
        lock.acquire()
    lock.release()


@_UNIX_ONLY
@pytest.mark.filterwarnings("default::UserWarning")
@pytest.mark.usefixtures("unsupported_flock")
def test_fallback_swaps_to_soft(tmp_path: Path) -> None:
    lock = UnixFileLock(tmp_path / "test.lock")

    with lock:
        assert lock.is_locked
        assert isinstance(lock, SoftFileLock)


@_UNIX_ONLY
@pytest.mark.filterwarnings("default::UserWarning")
@pytest.mark.usefixtures("unsupported_flock")
def test_fallback_writes_pid_and_hostname(tmp_path: Path) -> None:
    lock_path = tmp_path / "test.lock"

    with UnixFileLock(lock_path):
        assert lock_path.read_text(encoding="utf-8") == f"{os.getpid()}\n{socket.gethostname()}\n"


@_UNIX_ONLY
@pytest.mark.filterwarnings("default::UserWarning")
@pytest.mark.usefixtures("unsupported_flock")
def test_fallback_release_unlinks_file(tmp_path: Path) -> None:
    lock_path = tmp_path / "test.lock"
    lock = UnixFileLock(lock_path)

    lock.acquire()
    assert lock_path.exists()
    lock.release()
    assert not lock_path.exists()


@_UNIX_ONLY
@pytest.mark.filterwarnings("default::UserWarning")
def test_fallback_subsequent_acquire_skips_flock(tmp_path: Path, unsupported_flock: MagicMock) -> None:
    lock = UnixFileLock(tmp_path / "test.lock")

    lock.acquire()
    lock.release()
    unsupported_flock.reset_mock()

    lock.acquire()
    lock.release()
    unsupported_flock.assert_not_called()


@_UNIX_ONLY
@pytest.mark.filterwarnings("default::UserWarning")
@pytest.mark.usefixtures("unsupported_flock")
def test_fallback_reentrant_locking(tmp_path: Path) -> None:
    lock = UnixFileLock(tmp_path / "test.lock")

    with lock:
        with lock:
            assert lock.is_locked
        assert lock.is_locked
    assert not lock.is_locked


@_UNIX_ONLY
def test_release_suppresses_eio_on_close(tmp_path: Path, mocker: MockerFixture) -> None:
    lock = UnixFileLock(tmp_path / "test.lock")
    lock.acquire()

    real_close = os.close  # capture before the patch so the fd still closes for real
    fd_to_fail = lock._context.lock_file_fd  # _release() nulls this before closing, so read it now

    def _close_eio(fd: int) -> None:
        real_close(fd)
        if fd == fd_to_fail:
            raise OSError(EIO, "Input/output error")

    mocker.patch("filelock._unix.os.close", side_effect=_close_eio)
    lock.release()
    assert not lock.is_locked


@pytest.fixture
def unsupported_flock(mocker: MockerFixture) -> MagicMock:
    return mocker.patch("filelock._unix.fcntl.flock", side_effect=OSError(ENOSYS, "Function not implemented"))
