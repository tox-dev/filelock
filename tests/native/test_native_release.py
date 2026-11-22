from __future__ import annotations

from errno import EIO
from typing import TYPE_CHECKING

import pytest

from filelock import (
    FileLock,
)
from tests.capability_marks import (
    NEEDS_FCNTL,
    WINDOWS_ONLY,
)

if TYPE_CHECKING:
    from pathlib import Path

    from pytest_mock import MockerFixture


def test_release_keeps_lock_until_final_hold(tmp_path: Path) -> None:
    lock = FileLock(str(tmp_path / "a"))
    lock.acquire()
    lock.acquire()
    lock.release()
    assert lock.is_locked
    assert lock.lock_counter == 1
    lock.release()
    assert not lock.is_locked
    assert lock.lock_counter == 0


def test_forced_release_drops_all_holds(tmp_path: Path) -> None:
    lock = FileLock(str(tmp_path / "a"))
    lock.acquire()
    lock.acquire()
    lock.release(force=True)
    assert not lock.is_locked
    assert lock.lock_counter == 0


@NEEDS_FCNTL  # pragma: needs fcntl
def test_unix_release_keeps_lock_held_when_unlock_fails(tmp_path: Path, mocker: MockerFixture) -> None:
    lock = FileLock(str(tmp_path / "a"))
    lock.acquire()
    mocker.patch("filelock._native._unix.fcntl.flock", side_effect=[OSError(EIO, "unlock failed"), None], autospec=True)
    with pytest.raises(OSError, match="unlock failed"):
        lock.release()
    assert lock.is_locked
    assert lock.lock_counter == 1
    lock.release()
    assert not lock.is_locked


@NEEDS_FCNTL  # pragma: needs fcntl
def test_unix_context_exit_propagates_unlock_failure(tmp_path: Path, mocker: MockerFixture) -> None:
    mocker.patch("filelock._native._unix.unlock_fd", side_effect=[OSError(EIO, "unlock failed"), None], autospec=True)
    lock = FileLock(str(tmp_path / "a"))
    with pytest.raises(OSError, match="unlock failed"), lock:
        assert lock.is_locked
    assert lock.is_locked
    lock.release()
    assert not lock.is_locked


@WINDOWS_ONLY  # pragma: win32 cover
def test_windows_release_keeps_lock_held_when_unlock_fails(tmp_path: Path, mocker: MockerFixture) -> None:
    lock = FileLock(str(tmp_path / "a"))
    lock.acquire()
    mocker.patch(
        "filelock._native._windows.unlock_fd", side_effect=[OSError(EIO, "unlock failed"), None], autospec=True
    )
    with pytest.raises(OSError, match="unlock failed"):
        lock.release()
    assert lock.is_locked
    assert lock.lock_counter == 1
    lock.release()
    assert not lock.is_locked


@WINDOWS_ONLY  # pragma: win32 cover
def test_windows_close_failure_still_commits_release(tmp_path: Path, mocker: MockerFixture) -> None:
    lock = FileLock(str(tmp_path / "a"))
    lock.acquire()
    # Close can fail after the OS has already released the lock.
    mocker.patch("filelock._native._windows.os.close", side_effect=OSError(EIO, "close failed"), autospec=True)
    with pytest.raises(OSError, match="close failed"):
        lock.release()
    assert not lock.is_locked
    assert lock.lock_counter == 0
