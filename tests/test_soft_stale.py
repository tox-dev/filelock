from __future__ import annotations

import os
import socket
import sys
from errno import ENODEV, EPERM
from pathlib import Path
from typing import TYPE_CHECKING, Final

import pytest

from filelock import SoftFileLock
from filelock._soft import _MAX_LOCK_FILE_SIZE

if TYPE_CHECKING:
    from unittest.mock import MagicMock

    from pytest_mock import MockerFixture

_UNIX_ONLY: Final[pytest.MarkDecorator] = pytest.mark.skipif(
    sys.platform == "win32", reason="unix-only stale lock detection"
)
_WINDOWS_ONLY: Final[pytest.MarkDecorator] = pytest.mark.skipif(sys.platform != "win32", reason="windows-only")

_HOST: Final[str] = socket.gethostname()
_DEAD_PID: Final[int] = 2**22 + 1


@pytest.fixture
def lock_path(tmp_path: Path) -> Path:
    return tmp_path / "test.lock"


def _holder(pid: int, *, host: str = _HOST, creation_time: int | None = None) -> str:
    return "\n".join([str(pid), host, *([] if creation_time is None else [str(creation_time)])]) + "\n"


def test_lock_writes_pid_and_hostname(lock_path: Path) -> None:
    with SoftFileLock(lock_path):
        lines = lock_path.read_text(encoding="utf-8").strip().splitlines()
        if sys.platform == "win32":
            assert lines[:2] == [str(os.getpid()), _HOST]
            assert len(lines) == 3
            int(lines[2])  # creation FILETIME must parse as int
        else:
            assert lines == [str(os.getpid()), _HOST]


@pytest.mark.parametrize(
    "content",
    [
        pytest.param(_holder(_DEAD_PID), id="two_line"),
        pytest.param(_holder(_DEAD_PID, creation_time=123456789), id="three_line"),
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
    lock_path.write_text(_holder(_DEAD_PID, host="other-host.example.com"), encoding="utf-8")
    _assert_times_out(lock_path)


@_UNIX_ONLY
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
        pytest.param(b"not-a-pid\nhostname\n", id="two_line_bad_pid"),
        pytest.param(f"{_DEAD_PID}\nhostname\nnot-a-time\n".encode(), id="three_line_bad_creation_time"),
    ],
)
def test_unparseable_lock_evicted_when_old(lock_path: Path, content: bytes) -> None:
    lock_path.write_bytes(content)
    os.utime(lock_path, (0, 0))
    # An unreadable lock (bad line count, non-integer pid/creation time, empty, or oversized) must self-heal
    # instead of staying stuck; a matching line count alone does not make a file well-formed.
    _assert_self_heals(lock_path)


@pytest.mark.parametrize(
    "content",
    [
        pytest.param(b"not-a-pid\n", id="malformed"),
        pytest.param(b"", id="empty"),
        pytest.param(b"not-a-pid\nhostname\n", id="two_line_bad_pid"),
    ],
)
def test_unparseable_lock_not_evicted_when_fresh(lock_path: Path, content: bytes) -> None:
    lock_path.write_bytes(content)
    _assert_times_out(lock_path)


@pytest.mark.parametrize(
    "pid",
    [
        pytest.param(0, id="zero"),
        pytest.param(-1, id="negative"),
        pytest.param(2**31, id="oversized"),
    ],
)
def test_out_of_range_pid_self_heals_when_old(lock_path: Path, pid: int) -> None:
    lock_path.write_text(_holder(pid), encoding="utf-8")
    os.utime(lock_path, (0, 0))
    # pid 0 or -1 makes os.kill probe the caller's own process group (reads as alive), so the lock is never
    # reclaimed; an oversized pid raises OverflowError out of stale detection. Both are malformed and must
    # self-heal, matching what _parse_marker_bytes rejects.
    _assert_self_heals(lock_path)


def test_out_of_range_pid_not_evicted_when_fresh(lock_path: Path) -> None:
    lock_path.write_text(_holder(0), encoding="utf-8")
    # A fresh out-of-range pid is malformed too, but like any malformed lock it is left alone until it ages
    # past the threshold, so a peer mid-write is not mistaken for a stale lock.
    _assert_times_out(lock_path)


def test_stale_lock_rename_race(lock_path: Path, mocker: MockerFixture) -> None:
    lock_path.write_text(_holder(_DEAD_PID), encoding="utf-8")
    mocker.patch.object(SoftFileLock, "_is_process_alive", return_value=False)
    mocker.patch.object(Path, "rename", side_effect=FileNotFoundError("already gone"))
    _assert_times_out(lock_path)


@_UNIX_ONLY
def test_symlinked_lock_file_is_not_followed(tmp_path: Path, lock_path: Path) -> None:
    target = tmp_path / "target"
    target.write_text(_holder(99999), encoding="utf-8")
    lock_path.symlink_to(target)

    # Neither the pid read nor stale detection may follow the symlink onto the target file.
    assert SoftFileLock(lock_path).pid is None
    assert target.read_text(encoding="utf-8") == _holder(99999)


def test_fifo_lock_file_does_not_block(lock_path: Path) -> None:
    if sys.platform == "win32":
        pytest.skip("os.mkfifo is unix-only")
    # An attacker-placed FIFO must not stall the open; O_NONBLOCK makes the read bail instead of hang.
    os.mkfifo(lock_path)
    assert SoftFileLock(lock_path).pid is None


def test_fifo_lock_file_with_attached_writer_self_heals(lock_path: Path) -> None:
    if sys.platform == "win32":
        pytest.skip("os.mkfifo is unix-only")
    # A same-UID peer can plant a FIFO with a writer attached so a non-blocking read would raise EAGAIN. The lstat
    # guard classifies it as a malformed lock before any open, so an aged FIFO self-heals like any other node.
    os.mkfifo(lock_path)
    reader = os.open(lock_path, os.O_RDONLY | os.O_NONBLOCK)
    writer = os.open(lock_path, os.O_WRONLY | os.O_NONBLOCK)  # attached but never written, so reads get EAGAIN
    try:
        os.utime(lock_path, (0, 0))
        _assert_self_heals(lock_path)
    finally:
        os.close(reader)
        os.close(writer)


def test_socket_lock_file_self_heals(lock_path: Path) -> None:
    if sys.platform == "win32":
        pytest.skip("AF_UNIX sockets are unix-only")
    sock = socket.socket(socket.AF_UNIX)
    try:
        sock.bind(str(lock_path))
    except OSError:
        sock.close()
        pytest.skip("AF_UNIX path too long for this temp dir")
    # A Unix-domain socket cannot be os.open()ed as a file. Before the lstat guard the failed open was swallowed by
    # stale detection so acquisition wedged; an aged socket now self-heals like any other non-regular node.
    try:
        os.utime(lock_path, (0, 0))
        _assert_self_heals(lock_path)
    finally:
        sock.close()


def test_stale_detection_errors_suppressed(lock_path: Path, mocker: MockerFixture) -> None:
    lock_path.write_text(_holder(os.getpid()), encoding="utf-8")
    mock_read: MagicMock = mocker.patch("filelock._soft._read_lock_file", side_effect=OSError("read failed"))
    _assert_times_out(lock_path)
    mock_read.assert_called()


@_WINDOWS_ONLY
def test_windows_stale_lock_broken_when_pid_recycled(lock_path: Path, mocker: MockerFixture) -> None:
    lock_path.write_text(_holder(1234, creation_time=100000000), encoding="utf-8")
    mocker.patch.object(SoftFileLock, "_is_process_alive", return_value=True)
    mocker.patch.object(SoftFileLock, "_get_process_creation_time", return_value=999999999)
    _assert_self_heals(lock_path)


@_WINDOWS_ONLY
def test_windows_stale_lock_not_broken_same_creation_time(lock_path: Path, mocker: MockerFixture) -> None:
    creation_time = 100000000
    lock_path.write_text(_holder(1234, creation_time=creation_time), encoding="utf-8")
    mocker.patch.object(SoftFileLock, "_is_process_alive", return_value=True)
    mocker.patch.object(SoftFileLock, "_get_process_creation_time", return_value=creation_time)
    _assert_times_out(lock_path)


@_WINDOWS_ONLY
def test_windows_stale_lock_conservative_without_creation_time(lock_path: Path, mocker: MockerFixture) -> None:
    lock_path.write_text(_holder(1234), encoding="utf-8")
    mocker.patch.object(SoftFileLock, "_is_process_alive", return_value=True)
    _assert_times_out(lock_path)


@_WINDOWS_ONLY
def test_get_process_creation_time_returns_int_on_windows() -> None:
    result = SoftFileLock._get_process_creation_time(os.getpid())
    assert result is not None
    assert isinstance(result, int)
    assert result > 0


@_WINDOWS_ONLY
def test_get_process_creation_time_returns_none_for_dead_pid() -> None:
    assert SoftFileLock._get_process_creation_time(_DEAD_PID) is None


@_UNIX_ONLY
def test_get_process_creation_time_returns_none_on_unix() -> None:
    assert SoftFileLock._get_process_creation_time(os.getpid()) is None


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        pytest.param(None, None, id="no_file"),
        pytest.param(b"not-a-number\n", None, id="malformed"),
        pytest.param(b"\xff\xfe\n", None, id="non_utf8"),
        pytest.param(b"x" * (_MAX_LOCK_FILE_SIZE + 1), None, id="oversized"),
        pytest.param(b"42\n", None, id="single_line"),
        pytest.param(_holder(0).encode(), None, id="out_of_range_pid"),
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
        pytest.param(_holder(os.getpid(), host="other-host"), False, id="same_pid_different_host"),
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


def _assert_self_heals(lock_path: Path) -> None:
    lock = SoftFileLock(lock_path, timeout=1)
    with lock:
        assert lock.is_locked


def _assert_times_out(lock_path: Path) -> None:
    with pytest.raises(TimeoutError):
        SoftFileLock(lock_path, timeout=0.1).acquire()
