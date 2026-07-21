from __future__ import annotations

import gc
import os
import socket
import sys
import threading
from contextlib import contextmanager
from errno import EACCES, EINTR, ENODEV, ENOSPC, EPERM
from pathlib import Path
from typing import TYPE_CHECKING, Final

import pytest
from coverage_pragmas import CAPABILITIES

from filelock import CloseErrorPolicy, SoftFileLock
from filelock._identity import process_start_token
from filelock._soft import _MAX_LOCK_FILE_SIZE
from tests.capability_marks import NEEDS_POSIX_SIGNALS, NEEDS_SYMLINK, NEEDS_UNLINK_OPEN_FILE

if sys.version_info >= (3, 11):  # pragma: no cover (py311+)
    from builtins import ExceptionGroup  # pragma: >=3.11 cover
else:  # pragma: no cover (<py311)
    from exceptiongroup import ExceptionGroup

if TYPE_CHECKING:
    from collections.abc import Iterator
    from unittest.mock import MagicMock

    from pytest_mock import MockerFixture

_WINDOWS_ONLY: Final[pytest.MarkDecorator] = pytest.mark.skipif(sys.platform != "win32", reason="windows-only")

_HOST: Final[str] = socket.gethostname()
_DEAD_PID: Final[int] = 2**22 + 1
_WIN_ERROR_ACCESS_DENIED: Final[int] = 5
_WIN_ERROR_INVALID_PARAMETER: Final[int] = 87


@pytest.fixture
def lock_path(tmp_path: Path) -> Path:
    return tmp_path / "test.lock"


def _holder(pid: int, *, host: str = _HOST, start: int | None = None) -> str:
    return "\n".join([str(pid), host, *([] if start is None else [str(start)])]) + "\n"


def test_lock_writes_pid_and_hostname(lock_path: Path) -> None:
    with SoftFileLock(lock_path):
        lines = lock_path.read_text(encoding="utf-8").strip().splitlines()
        assert lines[:2] == [str(os.getpid()), _HOST]
        # Every platform with a proven start time writes it as an integer third line; one without writes two lines.
        if (token := process_start_token(os.getpid())) is not None:
            assert lines == [str(os.getpid()), _HOST, str(token)]
        else:  # pragma: no cover  # every CI platform exposes a start time
            assert lines == [str(os.getpid()), _HOST]


@pytest.mark.parametrize(
    "content",
    [
        pytest.param(_holder(_DEAD_PID), id="two_line"),
        pytest.param(_holder(_DEAD_PID, start=123456789), id="three_line"),
    ],
)
def test_stale_lock_broken_when_process_dead(lock_path: Path, mocker: MockerFixture, content: str) -> None:
    lock_path.write_text(content, encoding="utf-8")
    mocker.patch("filelock._identity.process_alive", return_value=False)
    _assert_self_heals(lock_path)


def test_stale_lock_not_broken_when_process_alive(lock_path: Path, mocker: MockerFixture) -> None:
    lock_path.write_text(_holder(os.getpid()), encoding="utf-8")
    mocker.patch("filelock._identity.process_alive", return_value=True)
    _assert_times_out(lock_path)


def test_stale_lock_not_broken_different_hostname(lock_path: Path) -> None:
    lock_path.write_text(_holder(_DEAD_PID, host="other-host.example.com"), encoding="utf-8")
    _assert_times_out(lock_path)


_REQUIRES_START_TOKEN: Final[pytest.MarkDecorator] = pytest.mark.skipif(
    process_start_token(os.getpid()) is None, reason="platform exposes no proven process start time"
)


@_REQUIRES_START_TOKEN
def test_recycled_pid_marker_self_heals(lock_path: Path) -> None:
    # A live PID (our own) whose recorded start token differs is a recycled PID: the process that wrote the marker is
    # gone even though the number is now in use. No mock: the real start token is read and found not to match.
    token = process_start_token(os.getpid())
    assert token is not None
    lock_path.write_text(_holder(os.getpid(), start=token + 1), encoding="utf-8")
    _assert_self_heals(lock_path)


@_REQUIRES_START_TOKEN
def test_live_process_with_matching_start_token_retained(lock_path: Path) -> None:
    # Our own PID and our own start token: the recorded owner is this live process, so the marker must stand.
    lock_path.write_text(_holder(os.getpid(), start=process_start_token(os.getpid())), encoding="utf-8")
    _assert_times_out(lock_path)


def test_live_process_without_start_token_falls_back_to_pid(lock_path: Path) -> None:
    # A two-line marker carries no start token, so liveness rests on the PID alone; a live PID is left held.
    lock_path.write_text(_holder(os.getpid()), encoding="utf-8")
    _assert_times_out(lock_path)


@NEEDS_POSIX_SIGNALS  # pragma: needs posix-signals
@pytest.mark.parametrize(
    "errno",
    [pytest.param(EPERM, id="eperm"), pytest.param(ENODEV, id="unexpected_device")],
)
def test_stale_lock_not_broken_on_kill_error(lock_path: Path, mocker: MockerFixture, errno: int) -> None:
    lock_path.write_text(_holder(99999), encoding="utf-8")
    mocker.patch("filelock._identity.os.kill", side_effect=OSError(errno, "kill failed"))
    _assert_times_out(lock_path)


@pytest.mark.parametrize(
    "content",
    [
        pytest.param(b"not-a-pid\n", id="malformed"),
        pytest.param(b"", id="empty"),
        pytest.param(b"x" * (_MAX_LOCK_FILE_SIZE + 1), id="oversized"),
        pytest.param(b"not-a-pid\nhostname\n", id="two_line_bad_pid"),
        pytest.param(f"{_DEAD_PID}\nhostname\nnot-a-token\n".encode(), id="three_line_bad_start_token"),
    ],
)
def test_unparseable_lock_evicted_when_old(lock_path: Path, content: bytes) -> None:
    lock_path.write_bytes(content)
    os.utime(lock_path, (0, 0))
    # An unreadable lock (bad line count, non-integer pid/start token, empty, or oversized) must self-heal
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
    mocker.patch("filelock._identity.process_alive", return_value=False)
    mocker.patch.object(Path, "rename", side_effect=FileNotFoundError("already gone"))
    _assert_times_out(lock_path)


@NEEDS_SYMLINK
def test_symlinked_lock_file_is_not_followed(tmp_path: Path, lock_path: Path) -> None:  # pragma: needs symlink
    target = tmp_path / "target"
    target.write_text(_holder(99999), encoding="utf-8")
    lock_path.symlink_to(target)

    # Neither the pid read nor stale detection may follow the symlink onto the target file.
    assert SoftFileLock(lock_path).pid is None
    assert target.read_text(encoding="utf-8") == _holder(99999)


def test_fifo_lock_file_does_not_block(lock_path: Path) -> None:
    if sys.platform == "win32" or not CAPABILITIES["fifo"]:  # pragma: win32 cover
        pytest.skip("os.mkfifo is unavailable")  # the platform arm also narrows so ty resolves os.mkfifo below
    # An attacker-placed FIFO must not stall the open; O_NONBLOCK makes the read bail instead of hang.
    os.mkfifo(lock_path)  # pragma: win32 no cover
    assert SoftFileLock(lock_path).pid is None  # pragma: win32 no cover


def test_fifo_lock_file_with_attached_writer_self_heals(lock_path: Path) -> None:
    if sys.platform == "win32" or not CAPABILITIES["fifo"]:  # pragma: win32 cover
        pytest.skip("os.mkfifo is unavailable")  # the platform arm also narrows so ty resolves os.mkfifo below
    # A same-UID peer can plant a FIFO with a writer attached so a non-blocking read would raise EAGAIN. The lstat
    # guard classifies it as a malformed lock before any open, so an aged FIFO self-heals like any other node.
    os.mkfifo(lock_path)  # pragma: win32 no cover
    reader = os.open(lock_path, os.O_RDONLY | os.O_NONBLOCK)  # pragma: win32 no cover
    writer = os.open(lock_path, os.O_WRONLY | os.O_NONBLOCK)  # pragma: win32 no cover
    try:  # pragma: win32 no cover
        os.utime(lock_path, (0, 0))
        _assert_self_heals(lock_path)
    finally:
        os.close(reader)  # pragma: win32 no cover
        os.close(writer)  # pragma: win32 no cover


def test_socket_lock_file_self_heals(lock_path: Path) -> None:
    if sys.platform == "win32":  # pragma: win32 cover
        pytest.skip("AF_UNIX sockets are unix-only")
    sock = socket.socket(socket.AF_UNIX)  # pragma: win32 no cover
    try:  # pragma: win32 no cover
        sock.bind(str(lock_path))
    except OSError:  # pragma: darwin cover
        sock.close()  # pragma: darwin cover
        pytest.skip("AF_UNIX path too long for this temp dir")  # pragma: darwin cover
    # A Unix-domain socket cannot be os.open()ed as a file. Before the lstat guard the failed open was swallowed by
    # stale detection so acquisition wedged; an aged socket now self-heals like any other non-regular node.
    try:  # pragma: linux cover
        os.utime(lock_path, (0, 0))
        _assert_self_heals(lock_path)
    finally:
        sock.close()  # pragma: linux cover


def test_stale_detection_errors_suppressed(lock_path: Path, mocker: MockerFixture) -> None:
    lock_path.write_text(_holder(os.getpid()), encoding="utf-8")
    mock_read: MagicMock = mocker.patch("filelock._soft._read_lock_file", side_effect=OSError("read failed"))
    _assert_times_out(lock_path)
    mock_read.assert_called()


@_WINDOWS_ONLY
@pytest.mark.parametrize(
    ("captured_error", "ambient_error", "acquires"),
    [
        pytest.param(_WIN_ERROR_INVALID_PARAMETER, _WIN_ERROR_ACCESS_DENIED, True, id="dead"),
        pytest.param(_WIN_ERROR_ACCESS_DENIED, _WIN_ERROR_INVALID_PARAMETER, False, id="access-denied"),
    ],
)
def test_windows_stale_lock_uses_captured_process_error(  # pragma: win32 cover
    lock_path: Path,
    mocker: MockerFixture,
    captured_error: int,
    ambient_error: int,
    *,
    acquires: bool,
) -> None:
    if sys.platform != "win32":  # pragma: win32 cover
        pytest.skip("windows-only")  # pragma: no cover  # win32-only test; this guard never runs
    import ctypes

    def fail_open_process(_access: int, _inherit_handle: bool, _pid: int) -> None:
        ctypes.set_last_error(captured_error)
        ctypes.windll.kernel32.SetLastError(ambient_error)

    # ctypes function pointers do not expose a Python signature for autospec.
    mocker.patch("filelock._identity._KERNEL32.OpenProcess", side_effect=fail_open_process)
    lock_path.write_text(_holder(_DEAD_PID), encoding="utf-8")
    if acquires:
        _assert_self_heals(lock_path)
    else:
        _assert_times_out(lock_path)


@_WINDOWS_ONLY
def test_windows_stale_lock_broken_when_pid_recycled(lock_path: Path) -> None:  # pragma: win32 cover
    process_marker = lock_path.with_suffix(".process")
    with SoftFileLock(process_marker):
        start = int(process_marker.read_text(encoding="utf-8").splitlines()[2])
    lock_path.write_text(_holder(os.getpid(), start=start + 1), encoding="utf-8")
    _assert_self_heals(lock_path)


@_WINDOWS_ONLY
def test_windows_live_process_marker_retained(lock_path: Path) -> None:  # pragma: win32 cover
    lock = SoftFileLock(lock_path)
    lock.acquire()
    try:
        _assert_times_out(lock_path)
    finally:
        lock.release()


@_WINDOWS_ONLY
def test_windows_live_process_marker_without_start_token_retained(lock_path: Path) -> None:  # pragma: win32 cover
    lock_path.write_text(_holder(os.getpid()), encoding="utf-8")
    _assert_times_out(lock_path)


@_WINDOWS_ONLY
def test_windows_process_probe_closes_handles(lock_path: Path) -> None:  # pragma: win32 cover
    lock = SoftFileLock(lock_path)
    lock.acquire()
    try:
        handle_count = _current_process_handle_count()
        for _attempt in range(50):
            _assert_times_out(lock_path, timeout=0)
        # A per-probe handle leak would add one handle per iteration, so ~50 over the loop. Assert no growth beyond a
        # small margin rather than exact equality, which another thread opening or closing a handle would break.
        assert _current_process_handle_count() <= handle_count + 5
    finally:
        lock.release()


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


def test_write_failure_rolls_back_acquire(lock_path: Path, mocker: MockerFixture) -> None:
    mocker.patch("filelock._util.os.write", side_effect=OSError(ENOSPC, "No space left on device"))

    lock = SoftFileLock(lock_path)
    with pytest.raises(OSError, match="No space left on device"):
        lock.acquire()
    assert not lock.is_locked
    assert not lock_path.exists()


def _assert_self_heals(lock_path: Path) -> None:
    lock = SoftFileLock(lock_path, timeout=1)
    with lock:
        assert lock.is_locked


def _assert_times_out(lock_path: Path, *, timeout: float = 0.1) -> None:
    with pytest.raises(TimeoutError):
        SoftFileLock(lock_path, timeout=timeout).acquire()


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


def _hold_reports_foreign_identity(mocker: MockerFixture) -> None:
    # Make the held descriptor report a different inode than the file on disk, as if a peer replaced the marker at the
    # path. fstat runs only on our own lock fd, so nothing else in acquire or release is disturbed.
    mocker.patch("filelock._soft.os.fstat", return_value=mocker.Mock(st_dev=1, st_ino=999))


def test_short_writes_still_write_the_whole_record(lock_path: Path, mocker: MockerFixture) -> None:
    real_write = os.write
    mocker.patch("filelock._util.os.write", side_effect=lambda fd, data: real_write(fd, bytes(data[:1])))

    with SoftFileLock(lock_path):
        lines = lock_path.read_text(encoding="utf-8").splitlines()
    assert lines[0] == str(os.getpid())
    assert lines[1] == _HOST


def test_zero_write_rolls_back_acquire(lock_path: Path, mocker: MockerFixture) -> None:
    mocker.patch("filelock._util.os.write", return_value=0)

    lock = SoftFileLock(lock_path)
    with pytest.raises(OSError, match="0 bytes"):
        lock.acquire()
    assert not lock.is_locked
    assert not lock_path.exists()


def test_failed_acquire_cleanup_spares_a_replacement(lock_path: Path, mocker: MockerFixture) -> None:
    mocker.patch("filelock._util.os.write", side_effect=OSError(ENOSPC, "No space left on device"))
    _hold_reports_foreign_identity(mocker)

    lock = SoftFileLock(lock_path)
    with pytest.raises(OSError, match="No space left on device"):
        lock.acquire()
    assert lock_path.exists()


def test_release_without_identity_skips_unlink(lock_path: Path, mocker: MockerFixture) -> None:
    lock = SoftFileLock(lock_path)
    lock.acquire()
    # The held descriptor can no longer be identified, so release cannot prove the path is still ours: it closes and
    # clears held state without unlinking rather than risk deleting a successor's marker.
    mocker.patch("filelock._soft.os.fstat", side_effect=OSError)
    lock.release()
    assert not lock.is_locked
    assert lock_path.exists()


def test_normal_release_removes_own_marker(lock_path: Path) -> None:
    with SoftFileLock(lock_path):
        assert lock_path.exists()
    assert not lock_path.exists()


def test_del_suppresses_a_release_error(lock_path: Path, mocker: MockerFixture) -> None:
    # A dropped, still-held lock is finalized by __del__. A release error there must not escape as an unraisable
    # exception during garbage collection, which pytest would fail on and attribute to an unrelated test. Finalizing
    # under a raising release and forcing collection here leaves the run clean only if __del__ swallows the error.
    lock = SoftFileLock(lock_path)
    lock.acquire()
    mocker.patch.object(SoftFileLock, "release", side_effect=OSError("release failed"))
    del lock
    gc.collect()


@NEEDS_UNLINK_OPEN_FILE
def test_stale_release_spares_a_successor(lock_path: Path) -> None:  # pragma: needs unlink-open-file
    holder = SoftFileLock(lock_path)
    holder.acquire()
    # A peer breaks the stale marker and installs its own at the same path; unlink first so it gets a fresh inode.
    lock_path.unlink()
    lock_path.write_text(_holder(_DEAD_PID), encoding="utf-8")
    holder.release()
    assert lock_path.exists()
    assert str(_DEAD_PID) in lock_path.read_text(encoding="utf-8")


@_WINDOWS_ONLY
def test_windows_release_spares_a_replacement(lock_path: Path, mocker: MockerFixture) -> None:  # pragma: win32 cover
    lock = SoftFileLock(lock_path)
    lock.acquire()
    _hold_reports_foreign_identity(mocker)
    lock.release()
    assert lock_path.exists()
    lock_path.unlink()


@pytest.mark.parametrize(
    ("policy", "surfaces"),
    [
        pytest.param("default", True, id="default"),
        pytest.param("raise", True, id="raise"),
        pytest.param("suppress", False, id="suppress"),
    ],
)
def test_close_error_policy_cleans_marker(
    lock_path: Path,
    mocker: MockerFixture,
    policy: CloseErrorPolicy,
    *,
    surfaces: bool,
) -> None:
    lock = SoftFileLock(lock_path, close_error_policy=policy)
    lock.acquire()
    with _close_after_commit(mocker) as (close_error, attempts):
        if surfaces:
            with pytest.raises(OSError, match="close failed") as info:
                lock.release()
            assert info.value is close_error
        else:
            lock.release()
    assert (len(attempts), lock.is_locked, lock.lock_counter, lock_path.exists()) == (1, False, 0, False)


@pytest.mark.parametrize(
    ("depth", "force"),
    [pytest.param(2, False, id="nested"), pytest.param(2, True, id="forced")],
)
def test_close_error_suppression_releases_once(
    lock_path: Path,
    mocker: MockerFixture,
    depth: int,
    *,
    force: bool,
) -> None:
    lock = SoftFileLock(lock_path, close_error_policy="suppress")
    for _acquisition in range(depth):
        lock.acquire()
    with _close_after_commit(mocker) as (_, attempts):
        if force:
            lock.release(force=True)
        else:
            for _release in range(depth):
                lock.release()
    assert (len(attempts), lock.is_locked, lock.lock_counter, lock_path.exists()) == (1, False, 0, False)


@pytest.mark.parametrize(
    "use_proxy",
    [pytest.param(False, id="direct"), pytest.param(True, id="proxy")],
)
def test_context_surfaces_close_error_after_cleanup(
    lock_path: Path,
    mocker: MockerFixture,
    *,
    use_proxy: bool,
) -> None:
    lock = SoftFileLock(lock_path)
    with (
        _close_after_commit(mocker) as (close_error, attempts),
        pytest.raises(OSError, match="close failed") as info,
        lock.acquire() if use_proxy else lock,
    ):
        pass
    assert (info.value, len(attempts), lock.is_locked, lock_path.exists()) == (close_error, 1, False, False)


def test_second_release_does_not_close_reused_descriptor(
    lock_path: Path,
    tmp_path: Path,
    mocker: MockerFixture,
) -> None:
    lock = SoftFileLock(lock_path)
    lock.acquire()
    with _close_after_commit(mocker) as (close_error, attempts), pytest.raises(OSError, match="close failed") as info:
        lock.release()
    assert info.value is close_error
    reused_fd = os.open(tmp_path / "reused", os.O_CREAT | os.O_WRONLY)
    assert reused_fd == attempts[0]
    try:
        lock.release()
        os.fstat(reused_fd)
    finally:
        os.close(reused_fd)


@NEEDS_UNLINK_OPEN_FILE  # pragma: needs unlink-open-file
def test_close_error_spares_successor_marker(lock_path: Path, mocker: MockerFixture) -> None:
    holder = SoftFileLock(lock_path)
    holder.acquire()
    lock_path.unlink()
    lock_path.write_text(_holder(_DEAD_PID), encoding="utf-8")
    with _close_after_commit(mocker) as (_, _attempts), pytest.raises(OSError, match="close failed"):
        holder.release()
    assert lock_path.read_text(encoding="utf-8") == _holder(_DEAD_PID)


def test_close_and_marker_cleanup_failures_are_grouped(lock_path: Path, mocker: MockerFixture) -> None:
    lock = SoftFileLock(lock_path)
    lock.acquire()
    cleanup_error = RuntimeError("cleanup failed")
    unlink_mock = mocker.patch("filelock._soft.Path.unlink", side_effect=cleanup_error)
    with _close_after_commit(mocker) as (close_error, _attempts), pytest.raises(ExceptionGroup) as info:
        lock.release()
    mocker.stop(unlink_mock)
    assert (
        info.value.message,
        info.value.exceptions,
        close_error.__context__,
        cleanup_error.__context__,
        lock.is_locked,
    ) == (
        "lock descriptor close and marker cleanup both failed",
        (close_error, cleanup_error),
        None,
        None,
        False,
    )


def test_close_after_commit_ignores_closes_from_other_threads(mocker: MockerFixture) -> None:
    # The patch binds os.close process-wide, so a descriptor closed on another thread must pass through cleanly and
    # stay out of the caller's recorded attempts rather than absorb the injected failure.
    closed: list[int] = []

    def close_a_pipe() -> None:
        read_fd, write_fd = os.pipe()
        os.close(read_fd)
        os.close(write_fd)  # a non-caller thread: passes through untouched rather than raising the injected error
        closed.extend((read_fd, write_fd))

    with _close_after_commit(mocker) as (_close_error, attempts):
        worker = threading.Thread(target=close_a_pipe)
        worker.start()
        worker.join()

    assert len(closed) == 2
    assert attempts == []


@contextmanager
def _close_after_commit(mocker: MockerFixture) -> Iterator[tuple[OSError, list[int]]]:
    real_close = os.close
    close_error = OSError(EINTR, "close failed")
    attempts: list[int] = []
    # close is an attribute of the shared os module, so this patch binds for every thread rather than just this one.
    # Leave another thread's close alone: counting it would inflate attempts, and failing it would break whatever
    # unrelated work happens to be closing a descriptor while this window is open.
    caller = threading.get_ident()

    def close(fd: int) -> None:
        real_close(fd)
        if threading.get_ident() != caller:
            return
        attempts.append(fd)
        raise close_error

    close_mock = mocker.patch("filelock._soft.os.close", side_effect=close)
    try:
        yield close_error, attempts
    finally:
        mocker.stop(close_mock)


def test_soft_windows_unlink_gives_up_after_every_attempt_is_denied(tmp_path: Path, mocker: MockerFixture) -> None:
    # Windows can still hold a handle just after close, so the marker unlink retries on EACCES. Deny every attempt to
    # prove the retry runs and then stops, and that the denial never escapes to the caller.
    from filelock._soft import _file_identity

    marker = tmp_path / "a"
    marker.write_text("x", encoding="utf-8")
    lock = SoftFileLock(marker)
    mocker.patch("filelock._soft.time.sleep")
    mocker.patch("filelock._soft.Path.unlink", side_effect=PermissionError(EACCES, "handle still open"))

    lock._windows_unlink_if_ours(_file_identity(os.lstat(marker)))

    assert marker.exists()
