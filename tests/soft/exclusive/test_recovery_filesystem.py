from __future__ import annotations

import os
import socket
import sys
from typing import TYPE_CHECKING, Final

import pytest

from filelock import SoftFileLock
from tests.capability_marks import (
    NEEDS_FIFO,
    NEEDS_STALE_RECLAIM,
    NEEDS_SYMLINK,
)

if TYPE_CHECKING:
    from pathlib import Path

    from pytest_mock import MockerFixture, MockType

from tests.soft.exclusive.recovery_helpers import (
    DEAD_PID,
    WINDOWS_ONLY,
    assert_self_heals,
    assert_times_out,
    holder_record,
)

_WIN_ERROR_ACCESS_DENIED: Final[int] = 5


_WIN_ERROR_INVALID_PARAMETER: Final[int] = 87


@NEEDS_SYMLINK
def test_symlinked_lock_file_is_not_followed(tmp_path: Path, lock_path: Path) -> None:  # pragma: needs symlink
    target = tmp_path / "target"
    target.write_text(holder_record(99999), encoding="utf-8")
    lock_path.symlink_to(target)

    # Neither the pid read nor stale detection may follow the symlink onto the target file.
    assert SoftFileLock(lock_path).pid is None
    assert target.read_text(encoding="utf-8") == holder_record(99999)


@NEEDS_FIFO
def test_fifo_lock_file_does_not_block(lock_path: Path) -> None:
    # An attacker-placed FIFO must not stall the open; O_NONBLOCK makes the read bail instead of hang.
    assert sys.platform != "win32"  # narrows os.mkfifo for the type checker  # pragma: win32 no cover
    os.mkfifo(lock_path)  # pragma: win32 no cover
    assert SoftFileLock(lock_path).pid is None  # pragma: win32 no cover


@pytest.mark.usefixtures("unchanged_past_grace")
@NEEDS_STALE_RECLAIM
@NEEDS_FIFO
def test_fifo_lock_file_with_attached_writer_self_heals(lock_path: Path) -> None:
    # An attached writer makes a nonblocking read of an empty FIFO raise EAGAIN.
    assert sys.platform != "win32"  # narrows os.mkfifo and O_NONBLOCK for the type checker  # pragma: win32 no cover
    os.mkfifo(lock_path)  # pragma: win32 no cover
    reader = os.open(lock_path, os.O_RDONLY | os.O_NONBLOCK)  # pragma: win32 no cover
    writer = os.open(lock_path, os.O_WRONLY | os.O_NONBLOCK)  # pragma: win32 no cover
    try:  # pragma: win32 no cover
        assert_self_heals(lock_path)
    finally:
        os.close(reader)  # pragma: win32 no cover
        os.close(writer)  # pragma: win32 no cover


@pytest.mark.usefixtures("unchanged_past_grace")
@NEEDS_STALE_RECLAIM
@pytest.mark.skipif(sys.platform == "win32", reason="AF_UNIX sockets are unix-only")
def test_socket_lock_file_self_heals(lock_path: Path) -> None:
    assert sys.platform != "win32"  # narrows socket.AF_UNIX for the type checker  # pragma: win32 no cover
    sock = socket.socket(socket.AF_UNIX)  # pragma: win32 no cover
    try:  # pragma: win32 no cover
        sock.bind(str(lock_path))
    except OSError:  # pragma: darwin cover
        sock.close()  # pragma: darwin cover
        pytest.skip("AF_UNIX path too long for this temp dir")  # pragma: darwin cover
    # A Unix socket has no readable lock record.
    try:  # pragma: linux cover
        assert_self_heals(lock_path)
    finally:
        sock.close()  # pragma: linux cover


def test_stale_detection_errors_suppressed(lock_path: Path, mocker: MockerFixture) -> None:
    lock_path.write_text(holder_record(os.getpid()), encoding="utf-8")
    mock_read: MockType = mocker.patch(
        "filelock._soft._exclusive._recovery.read_lock_file", side_effect=OSError("read failed"), autospec=True
    )
    assert_times_out(lock_path)
    mock_read.assert_called()


@WINDOWS_ONLY
def test_windows_stale_lock_uses_captured_process_error(  # pragma: win32 cover
    lock_path: Path, mocker: MockerFixture
) -> None:
    _fail_open_process(mocker, _WIN_ERROR_INVALID_PARAMETER, _WIN_ERROR_ACCESS_DENIED)
    lock_path.write_text(holder_record(DEAD_PID), encoding="utf-8")
    assert_self_heals(lock_path)


def _fail_open_process(mocker: MockerFixture, captured_error: int, ambient_error: int) -> None:  # pragma: win32 cover
    if sys.platform != "win32":  # pragma: win32 cover  # narrows ctypes.windll for the type checker
        pytest.skip("windows-only")  # pragma: win32 no cover
    import ctypes

    def fail_open_process(_access: int, _inherit_handle: bool, _pid: int) -> None:
        ctypes.set_last_error(captured_error)
        ctypes.windll.kernel32.SetLastError(ambient_error)

    # ctypes function pointers do not expose a Python signature for autospec.
    mocker.patch("filelock._identity._process._KERNEL32.OpenProcess", side_effect=fail_open_process, autospec=True)


@WINDOWS_ONLY
def test_windows_stale_lock_held_on_access_denied(  # pragma: win32 cover
    lock_path: Path, mocker: MockerFixture
) -> None:
    _fail_open_process(mocker, _WIN_ERROR_ACCESS_DENIED, _WIN_ERROR_INVALID_PARAMETER)
    lock_path.write_text(holder_record(DEAD_PID), encoding="utf-8")
    assert_times_out(lock_path)


@WINDOWS_ONLY
def test_windows_stale_lock_broken_when_pid_recycled(lock_path: Path) -> None:  # pragma: win32 cover
    process_marker = lock_path.with_suffix(".process")
    with SoftFileLock(process_marker):
        start = int(process_marker.read_text(encoding="utf-8").splitlines()[2])
    lock_path.write_text(holder_record(os.getpid(), start=start + 1), encoding="utf-8")
    assert_self_heals(lock_path)


@WINDOWS_ONLY
def test_windows_live_process_marker_retained(lock_path: Path) -> None:  # pragma: win32 cover
    lock = SoftFileLock(lock_path)
    lock.acquire()
    try:
        assert_times_out(lock_path)
    finally:
        lock.release()


@WINDOWS_ONLY
def test_windows_live_process_marker_without_start_token_retained(lock_path: Path) -> None:  # pragma: win32 cover
    lock_path.write_text(holder_record(os.getpid()), encoding="utf-8")
    assert_times_out(lock_path)


@WINDOWS_ONLY
def test_windows_process_probe_closes_handles(lock_path: Path) -> None:  # pragma: win32 cover
    lock = SoftFileLock(lock_path)
    lock.acquire()
    try:
        handle_count = _current_process_handle_count()
        for _attempt in range(50):
            assert_times_out(lock_path, timeout=0)
        # Other threads can open or close handles during the sample.
        assert _current_process_handle_count() <= handle_count + 5
    finally:
        lock.release()


def _current_process_handle_count() -> int:  # pragma: win32 cover
    if sys.platform != "win32":  # pragma: win32 cover
        pytest.skip("windows-only")  # pragma: no cover  # only ever called on win32, so this guard never runs
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetCurrentProcess.argtypes = []
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.GetProcessHandleCount.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel32.GetProcessHandleCount.restype = wintypes.BOOL
    count = wintypes.DWORD()
    assert kernel32.GetProcessHandleCount(kernel32.GetCurrentProcess(), ctypes.byref(count)), ctypes.WinError(
        ctypes.get_last_error()
    )
    return count.value
