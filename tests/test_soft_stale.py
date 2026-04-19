from __future__ import annotations

import os
import socket
import sys
from errno import ENODEV, EPERM
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from filelock import SoftFileLock

if TYPE_CHECKING:
    from unittest.mock import MagicMock

    from pytest_mock import MockerFixture

unix_only = pytest.mark.skipif(sys.platform == "win32", reason="unix-only stale lock detection")
win_only = pytest.mark.skipif(sys.platform != "win32", reason="windows-only")


def test_lock_writes_pid_and_hostname(tmp_path: Path) -> None:
    lock_path = tmp_path / "test.lock"
    lock = SoftFileLock(lock_path)
    with lock:
        content = lock_path.read_text(encoding="utf-8")
        lines = content.strip().splitlines()
        assert lines[0] == str(os.getpid())
        assert lines[1] == socket.gethostname()
        if sys.platform == "win32":
            assert len(lines) == 3
            int(lines[2])  # must be parseable as int (creation FILETIME)
        else:
            assert len(lines) == 2


def test_stale_lock_broken_when_process_dead(tmp_path: Path, mocker: MockerFixture) -> None:
    lock_path = tmp_path / "test.lock"
    dead_pid = 2**22 + 1
    lock_path.write_text(f"{dead_pid}\n{socket.gethostname()}\n", encoding="utf-8")

    mocker.patch.object(SoftFileLock, "_is_process_alive", return_value=False)

    lock = SoftFileLock(lock_path, timeout=1)
    with lock:
        assert lock.is_locked


def test_stale_lock_not_broken_when_process_alive(tmp_path: Path, mocker: MockerFixture) -> None:
    lock_path = tmp_path / "test.lock"
    lock_path.write_text(f"{os.getpid()}\n{socket.gethostname()}\n", encoding="utf-8")

    mocker.patch.object(SoftFileLock, "_is_process_alive", return_value=True)

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


def test_stale_lock_malformed_evicted_when_old(tmp_path: Path) -> None:
    lock_path = tmp_path / "test.lock"
    lock_path.write_text("not-a-pid\n", encoding="utf-8")
    os.utime(lock_path, (0, 0))

    lock = SoftFileLock(lock_path, timeout=1)
    with lock:
        assert lock.is_locked


def test_stale_lock_malformed_not_evicted_when_fresh(tmp_path: Path) -> None:
    lock_path = tmp_path / "test.lock"
    lock_path.write_text("not-a-pid\n", encoding="utf-8")

    lock = SoftFileLock(lock_path, timeout=0.1)
    with pytest.raises(TimeoutError):
        lock.acquire()


def test_stale_lock_empty_file_evicted_when_old(tmp_path: Path) -> None:
    lock_path = tmp_path / "test.lock"
    lock_path.write_text("", encoding="utf-8")
    os.utime(lock_path, (0, 0))

    lock = SoftFileLock(lock_path, timeout=1)
    with lock:
        assert lock.is_locked


def test_stale_lock_empty_file_not_evicted_when_fresh(tmp_path: Path) -> None:
    lock_path = tmp_path / "test.lock"
    lock_path.write_text("", encoding="utf-8")

    lock = SoftFileLock(lock_path, timeout=0.1)
    with pytest.raises(TimeoutError):
        lock.acquire()


def test_stale_lock_rename_race(tmp_path: Path, mocker: MockerFixture) -> None:
    lock_path = tmp_path / "test.lock"
    dead_pid = 2**22 + 1
    lock_path.write_text(f"{dead_pid}\n{socket.gethostname()}\n", encoding="utf-8")

    mocker.patch.object(SoftFileLock, "_is_process_alive", return_value=False)
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

    mock_read: MagicMock = mocker.patch.object(Path, "read_text", side_effect=OSError("read failed"))

    lock = SoftFileLock(lock_path, timeout=0.1)
    with pytest.raises(TimeoutError):
        lock.acquire()
    mock_read.assert_called()


def test_stale_lock_three_line_format_accepted(tmp_path: Path, mocker: MockerFixture) -> None:
    lock_path = tmp_path / "test.lock"
    dead_pid = 2**22 + 1
    lock_path.write_text(f"{dead_pid}\n{socket.gethostname()}\n123456789\n", encoding="utf-8")

    mocker.patch.object(SoftFileLock, "_is_process_alive", return_value=False)

    lock = SoftFileLock(lock_path, timeout=1)
    with lock:
        assert lock.is_locked


@win_only
def test_windows_stale_lock_broken_when_pid_recycled(tmp_path: Path, mocker: MockerFixture) -> None:
    lock_path = tmp_path / "test.lock"
    recycled_pid = 1234
    original_creation_time = 100000000
    new_creation_time = 999999999

    lock_path.write_text(
        f"{recycled_pid}\n{socket.gethostname()}\n{original_creation_time}\n",
        encoding="utf-8",
    )

    mocker.patch.object(SoftFileLock, "_is_process_alive", return_value=True)
    mocker.patch.object(SoftFileLock, "_get_process_creation_time", return_value=new_creation_time)

    lock = SoftFileLock(lock_path, timeout=1)
    with lock:
        assert lock.is_locked


@win_only
def test_windows_stale_lock_not_broken_same_creation_time(tmp_path: Path, mocker: MockerFixture) -> None:
    lock_path = tmp_path / "test.lock"
    alive_pid = 1234
    creation_time = 100000000

    lock_path.write_text(
        f"{alive_pid}\n{socket.gethostname()}\n{creation_time}\n",
        encoding="utf-8",
    )

    mocker.patch.object(SoftFileLock, "_is_process_alive", return_value=True)
    mocker.patch.object(SoftFileLock, "_get_process_creation_time", return_value=creation_time)

    lock = SoftFileLock(lock_path, timeout=0.1)
    with pytest.raises(TimeoutError):
        lock.acquire()


@win_only
def test_windows_stale_lock_conservative_without_creation_time(tmp_path: Path, mocker: MockerFixture) -> None:
    lock_path = tmp_path / "test.lock"
    alive_pid = 1234
    lock_path.write_text(f"{alive_pid}\n{socket.gethostname()}\n", encoding="utf-8")

    mocker.patch.object(SoftFileLock, "_is_process_alive", return_value=True)

    lock = SoftFileLock(lock_path, timeout=0.1)
    with pytest.raises(TimeoutError):
        lock.acquire()


@win_only
def test_get_process_creation_time_returns_int_on_windows() -> None:
    result = SoftFileLock._get_process_creation_time(os.getpid())
    assert result is not None
    assert isinstance(result, int)
    assert result > 0


@win_only
def test_get_process_creation_time_returns_none_for_dead_pid() -> None:
    result = SoftFileLock._get_process_creation_time(2**22 + 1)
    assert result is None


@pytest.mark.skipif(sys.platform == "win32", reason="unix-only")
def test_get_process_creation_time_returns_none_on_unix() -> None:
    assert SoftFileLock._get_process_creation_time(os.getpid()) is None


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        pytest.param(None, None, id="no_file"),
        pytest.param("not-a-number\n", None, id="malformed"),
        pytest.param(f"{os.getpid()}\n{socket.gethostname()}\n", os.getpid(), id="valid"),
    ],
)
def test_pid(tmp_path: Path, content: str | None, expected: int | None) -> None:
    lock_path = tmp_path / "test.lock"
    if content is not None:
        lock_path.write_text(content, encoding="utf-8")
    assert SoftFileLock(lock_path).pid == expected


def test_pid_while_locked(tmp_path: Path) -> None:
    lock_path = tmp_path / "test.lock"
    lock = SoftFileLock(lock_path)
    with lock:
        assert lock.pid == os.getpid()


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        pytest.param(None, False, id="no_file"),
        pytest.param(f"{os.getpid() + 1}\n{socket.gethostname()}\n", False, id="different_pid"),
        pytest.param(f"{os.getpid()}\n{socket.gethostname()}\n", True, id="same_pid"),
    ],
)
def test_is_lock_held_by_us(tmp_path: Path, content: str | None, expected: bool) -> None:
    lock_path = tmp_path / "test.lock"
    if content is not None:
        lock_path.write_text(content, encoding="utf-8")
    assert SoftFileLock(lock_path).is_lock_held_by_us is expected


@pytest.mark.parametrize(
    "exists",
    [pytest.param(True, id="exists"), pytest.param(False, id="missing")],
)
def test_break_lock(tmp_path: Path, *, exists: bool) -> None:
    lock_path = tmp_path / "test.lock"
    if exists:
        lock_path.write_text(f"{os.getpid()}\n{socket.gethostname()}\n", encoding="utf-8")
    SoftFileLock(lock_path).break_lock()
    assert not lock_path.exists()


def test_write_lock_info_errors_suppressed(tmp_path: Path, mocker: MockerFixture) -> None:
    lock_path = tmp_path / "test.lock"
    mocker.patch("filelock._soft.os.write", side_effect=OSError("write failed"))

    lock = SoftFileLock(lock_path)
    with lock:
        assert lock.is_locked
        assert not lock_path.read_text(encoding="utf-8")
