from __future__ import annotations

import os
import socket
import sys
from errno import ENODEV, EPERM, ESRCH
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from filelock import SoftFileLock

if TYPE_CHECKING:
    from unittest.mock import MagicMock

    from pytest_mock import MockerFixture

unix_only = pytest.mark.skipif(sys.platform == "win32", reason="uses os.kill for process liveness check")


def test_lock_writes_pid_and_hostname(tmp_path: Path) -> None:
    lock_path = tmp_path / "test.lock"
    lock = SoftFileLock(lock_path)
    with lock:
        content = lock_path.read_text(encoding="utf-8")
        assert content == f"{os.getpid()}\n{socket.gethostname()}\n"


@unix_only
def test_stale_lock_broken_when_process_dead(tmp_path: Path, mocker: MockerFixture) -> None:
    lock_path = tmp_path / "test.lock"
    dead_pid = 2**22 + 1
    lock_path.write_text(f"{dead_pid}\n{socket.gethostname()}\n", encoding="utf-8")

    mocker.patch("filelock._soft.os.kill", side_effect=OSError(ESRCH, "No such process"))

    lock = SoftFileLock(lock_path, timeout=1)
    with lock:
        assert lock.is_locked


def test_stale_lock_not_broken_when_process_alive(tmp_path: Path) -> None:
    lock_path = tmp_path / "test.lock"
    lock_path.write_text(f"{os.getpid()}\n{socket.gethostname()}\n", encoding="utf-8")

    lock = SoftFileLock(lock_path, timeout=0.1)
    with pytest.raises(TimeoutError):
        lock.acquire()


def test_stale_lock_not_broken_different_hostname(tmp_path: Path) -> None:
    lock_path = tmp_path / "test.lock"
    dead_pid = 2**22 + 1
    lock_path.write_text(f"{dead_pid}\nother-host.example.com\n", encoding="utf-8")

    lock = SoftFileLock(lock_path, timeout=0.1)
    with pytest.raises(TimeoutError):
        lock.acquire()


@unix_only
def test_stale_lock_not_broken_when_eperm(tmp_path: Path, mocker: MockerFixture) -> None:
    lock_path = tmp_path / "test.lock"
    lock_path.write_text(f"{99999}\n{socket.gethostname()}\n", encoding="utf-8")

    mocker.patch("filelock._soft.os.kill", side_effect=OSError(EPERM, "Operation not permitted"))

    lock = SoftFileLock(lock_path, timeout=0.1)
    with pytest.raises(TimeoutError):
        lock.acquire()


def test_stale_lock_empty_file_ignored(tmp_path: Path) -> None:
    lock_path = tmp_path / "test.lock"
    lock_path.write_text("", encoding="utf-8")

    lock = SoftFileLock(lock_path, timeout=0.1)
    with pytest.raises(TimeoutError):
        lock.acquire()


def test_stale_lock_malformed_content_ignored(tmp_path: Path) -> None:
    lock_path = tmp_path / "test.lock"
    lock_path.write_text("not-a-pid\n", encoding="utf-8")

    lock = SoftFileLock(lock_path, timeout=0.1)
    with pytest.raises(TimeoutError):
        lock.acquire()


@unix_only
def test_stale_lock_rename_race(tmp_path: Path, mocker: MockerFixture) -> None:
    lock_path = tmp_path / "test.lock"
    dead_pid = 2**22 + 1
    lock_path.write_text(f"{dead_pid}\n{socket.gethostname()}\n", encoding="utf-8")

    mocker.patch("filelock._soft.os.kill", side_effect=OSError(ESRCH, "No such process"))
    mocker.patch.object(Path, "rename", side_effect=FileNotFoundError("already gone"))

    lock = SoftFileLock(lock_path, timeout=0.1)
    with pytest.raises(TimeoutError):
        lock.acquire()


@unix_only
def test_stale_lock_unexpected_kill_error_suppressed(tmp_path: Path, mocker: MockerFixture) -> None:
    lock_path = tmp_path / "test.lock"
    lock_path.write_text(f"{99999}\n{socket.gethostname()}\n", encoding="utf-8")

    mocker.patch("filelock._soft.os.kill", side_effect=OSError(ENODEV, "No such device"))

    lock = SoftFileLock(lock_path, timeout=0.1)
    with pytest.raises(TimeoutError):
        lock.acquire()


def test_stale_detection_errors_suppressed(tmp_path: Path, mocker: MockerFixture) -> None:
    lock_path = tmp_path / "test.lock"
    lock_path.write_text(f"{os.getpid()}\n{socket.gethostname()}\n", encoding="utf-8")

    mock_read: MagicMock = mocker.patch.object(SoftFileLock, "_read_lock_info", side_effect=OSError("read failed"))

    lock = SoftFileLock(lock_path, timeout=0.1)
    with pytest.raises(TimeoutError):
        lock.acquire()
    mock_read.assert_called()


def test_write_lock_info_errors_suppressed(tmp_path: Path, mocker: MockerFixture) -> None:
    lock_path = tmp_path / "test.lock"
    mocker.patch("filelock._soft.os.write", side_effect=OSError("write failed"))

    lock = SoftFileLock(lock_path)
    with lock:
        assert lock.is_locked
        assert not lock_path.read_text(encoding="utf-8")
