from __future__ import annotations

import os
import socket
import sys
from errno import ENODEV, EPERM
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from filelock import SoftFileLock
from filelock._soft import _MAX_LOCK_FILE_SIZE

if TYPE_CHECKING:
    from unittest.mock import MagicMock

    from pytest_mock import MockerFixture

unix_only = pytest.mark.skipif(sys.platform == "win32", reason="unix-only stale lock detection")
win_only = pytest.mark.skipif(sys.platform != "win32", reason="windows-only")

HOST = socket.gethostname()
DEAD_PID = 2**22 + 1


@pytest.fixture
def lock_path(tmp_path: Path) -> Path:
    return tmp_path / "test.lock"


def _holder(pid: int, *, host: str = HOST, creation_time: int | None = None) -> str:
    lines = [str(pid), host, *([] if creation_time is None else [str(creation_time)])]
    return "\n".join(lines) + "\n"


def _assert_self_heals(lock_path: Path) -> None:
    lock = SoftFileLock(lock_path, timeout=1)
    with lock:
        assert lock.is_locked


def _assert_times_out(lock_path: Path) -> None:
    lock = SoftFileLock(lock_path, timeout=0.1)
    with pytest.raises(TimeoutError):
        lock.acquire()


def test_lock_writes_pid_and_hostname(lock_path: Path) -> None:
    lock = SoftFileLock(lock_path)
    with lock:
        lines = lock_path.read_text(encoding="utf-8").strip().splitlines()
        assert lines[0] == str(os.getpid())
        assert lines[1] == HOST
        if sys.platform == "win32":
            assert len(lines) == 3
            int(lines[2])  # must be parseable as int (creation FILETIME)
        else:
            assert len(lines) == 2


@pytest.mark.parametrize(
    "content",
    [
        pytest.param(_holder(DEAD_PID), id="two_line"),
        pytest.param(_holder(DEAD_PID, creation_time=123456789), id="three_line"),
    ],
)
def test_stale_lock_broken_when_process_dead(lock_path: Path, mocker: MockerFixture, content: str) -> None:
    lock_path.write_text(content, encoding="utf-8")
    mocker.patch.object(SoftFileLock, "_is_process_alive", return_value=False)
    _assert_self_heals(lock_path)


def test_stale_lock_not_broken_when_process_alive(lock_path: Path, mocker: MockerFixture) -> None:
    lock_path.write_text(_holder(os.getpid()), encoding="utf-8")
    mocker.patch.object(SoftFileLock, "_is_process_alive", return_value=True)
    _assert_times_out(lock_path)


def test_stale_lock_not_broken_different_hostname(lock_path: Path) -> None:
    lock_path.write_text(_holder(DEAD_PID, host="other-host.example.com"), encoding="utf-8")
    _assert_times_out(lock_path)


@unix_only
@pytest.mark.parametrize(
    "errno",
    [pytest.param(EPERM, id="eperm"), pytest.param(ENODEV, id="unexpected_device")],
)
def test_stale_lock_not_broken_on_kill_error(lock_path: Path, mocker: MockerFixture, errno: int) -> None:
    lock_path.write_text(_holder(99999), encoding="utf-8")
    mocker.patch("filelock._soft.os.kill", side_effect=OSError(errno, "kill failed"))
    _assert_times_out(lock_path)


@pytest.mark.parametrize(
    "content",
    [
        pytest.param(b"not-a-pid\n", id="malformed"),
        pytest.param(b"", id="empty"),
        pytest.param(b"x" * (_MAX_LOCK_FILE_SIZE + 1), id="oversized"),
    ],
)
def test_unparseable_lock_evicted_when_old(lock_path: Path, content: bytes) -> None:
    lock_path.write_bytes(content)
    os.utime(lock_path, (0, 0))
    # An unreadable lock (malformed, empty, or oversized) must self-heal rather than stay stuck forever.
    _assert_self_heals(lock_path)


@pytest.mark.parametrize(
    "content",
    [pytest.param(b"not-a-pid\n", id="malformed"), pytest.param(b"", id="empty")],
)
def test_unparseable_lock_not_evicted_when_fresh(lock_path: Path, content: bytes) -> None:
    lock_path.write_bytes(content)
    _assert_times_out(lock_path)


def test_stale_lock_rename_race(lock_path: Path, mocker: MockerFixture) -> None:
    lock_path.write_text(_holder(DEAD_PID), encoding="utf-8")
    mocker.patch.object(SoftFileLock, "_is_process_alive", return_value=False)
    mocker.patch.object(Path, "rename", side_effect=FileNotFoundError("already gone"))
    _assert_times_out(lock_path)


@unix_only
def test_symlinked_lock_file_is_not_followed(tmp_path: Path, lock_path: Path) -> None:
    target = tmp_path / "target"
    target.write_text(_holder(99999), encoding="utf-8")
    lock_path.symlink_to(target)

    # Neither the pid read nor stale detection may follow the symlink onto the target file.
    assert SoftFileLock(lock_path).pid is None
    assert target.read_text(encoding="utf-8") == _holder(99999)


@unix_only
def test_fifo_lock_file_does_not_block(lock_path: Path) -> None:
    getattr(os, "mkfifo")(lock_path)  # noqa: B009 # os.mkfifo is unix-only; getattr keeps the win32 type check happy
    # An attacker-placed FIFO must not stall the open; O_NONBLOCK makes the read bail instead of hang.
    assert SoftFileLock(lock_path).pid is None


def test_stale_detection_errors_suppressed(lock_path: Path, mocker: MockerFixture) -> None:
    lock_path.write_text(_holder(os.getpid()), encoding="utf-8")
    mock_read: MagicMock = mocker.patch("filelock._soft._read_lock_file", side_effect=OSError("read failed"))
    _assert_times_out(lock_path)
    mock_read.assert_called()


@win_only
def test_windows_stale_lock_broken_when_pid_recycled(lock_path: Path, mocker: MockerFixture) -> None:
    lock_path.write_text(_holder(1234, creation_time=100000000), encoding="utf-8")
    mocker.patch.object(SoftFileLock, "_is_process_alive", return_value=True)
    mocker.patch.object(SoftFileLock, "_get_process_creation_time", return_value=999999999)
    _assert_self_heals(lock_path)


@win_only
def test_windows_stale_lock_not_broken_same_creation_time(lock_path: Path, mocker: MockerFixture) -> None:
    creation_time = 100000000
    lock_path.write_text(_holder(1234, creation_time=creation_time), encoding="utf-8")
    mocker.patch.object(SoftFileLock, "_is_process_alive", return_value=True)
    mocker.patch.object(SoftFileLock, "_get_process_creation_time", return_value=creation_time)
    _assert_times_out(lock_path)


@win_only
def test_windows_stale_lock_conservative_without_creation_time(lock_path: Path, mocker: MockerFixture) -> None:
    lock_path.write_text(_holder(1234), encoding="utf-8")
    mocker.patch.object(SoftFileLock, "_is_process_alive", return_value=True)
    _assert_times_out(lock_path)


@win_only
def test_get_process_creation_time_returns_int_on_windows() -> None:
    result = SoftFileLock._get_process_creation_time(os.getpid())
    assert result is not None
    assert isinstance(result, int)
    assert result > 0


@win_only
def test_get_process_creation_time_returns_none_for_dead_pid() -> None:
    assert SoftFileLock._get_process_creation_time(DEAD_PID) is None


@pytest.mark.skipif(sys.platform == "win32", reason="unix-only")
def test_get_process_creation_time_returns_none_on_unix() -> None:
    assert SoftFileLock._get_process_creation_time(os.getpid()) is None


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        pytest.param(None, None, id="no_file"),
        pytest.param(b"not-a-number\n", None, id="malformed"),
        pytest.param(b"\xff\xfe\n", None, id="non_utf8"),
        pytest.param(b"x" * (_MAX_LOCK_FILE_SIZE + 1), None, id="oversized"),
        pytest.param(_holder(os.getpid()).encode(), os.getpid(), id="valid"),
    ],
)
def test_pid(lock_path: Path, content: bytes | None, expected: int | None) -> None:
    if content is not None:
        lock_path.write_bytes(content)
    assert SoftFileLock(lock_path).pid == expected


def test_pid_while_locked(lock_path: Path) -> None:
    lock = SoftFileLock(lock_path)
    with lock:
        assert lock.pid == os.getpid()


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        pytest.param(None, False, id="no_file"),
        pytest.param(_holder(os.getpid() + 1), False, id="different_pid"),
        pytest.param(_holder(os.getpid()), True, id="same_pid"),
    ],
)
def test_is_lock_held_by_us(lock_path: Path, content: str | None, expected: bool) -> None:
    if content is not None:
        lock_path.write_text(content, encoding="utf-8")
    assert SoftFileLock(lock_path).is_lock_held_by_us is expected


@pytest.mark.parametrize(
    "exists",
    [pytest.param(True, id="exists"), pytest.param(False, id="missing")],
)
def test_break_lock(lock_path: Path, *, exists: bool) -> None:
    if exists:
        lock_path.write_text(_holder(os.getpid()), encoding="utf-8")
    SoftFileLock(lock_path).break_lock()
    assert not lock_path.exists()


def test_write_lock_info_errors_suppressed(lock_path: Path, mocker: MockerFixture) -> None:
    mocker.patch("filelock._soft.os.write", side_effect=OSError("write failed"))

    lock = SoftFileLock(lock_path)
    with lock:
        assert lock.is_locked
        assert not lock_path.read_text(encoding="utf-8")
