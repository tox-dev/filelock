from __future__ import annotations

import os
import stat
import sys
from errno import EBADF, EIO, ENOSYS
from typing import TYPE_CHECKING

import pytest

from filelock import SoftFileLock, Timeout, UnixFileLock
from tests.capability_marks import NEEDS_FCNTL, NEEDS_STALE_RECLAIM

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from pytest_mock import MockerFixture


@NEEDS_FCNTL
@pytest.mark.filterwarnings("ignore:flock not supported on this filesystem:UserWarning")
@pytest.mark.usefixtures("unsupported_flock")
def test_fallback_publishes_in_the_file_it_created(tmp_path: Path) -> None:  # pragma: needs fcntl
    assert sys.platform != "win32"
    lock = UnixFileLock(tmp_path / "test.lock")

    lock.acquire(timeout=0)
    assert lock.is_locked
    marker = lock
    assert (marker.pid, marker.is_lock_held_by_us) == (os.getpid(), True)
    assert (tmp_path / "test.lock").read_bytes().startswith(f"{os.getpid()}\n".encode())
    lock.release()


@NEEDS_FCNTL
@pytest.mark.filterwarnings("ignore:flock not supported on this filesystem:UserWarning")
@pytest.mark.usefixtures("unsupported_flock")
def test_fallback_write_error_leaves_marker(tmp_path: Path, mocker: MockerFixture) -> None:  # pragma: needs fcntl
    lock_path = tmp_path / "test.lock"
    mocker.patch("os.write", side_effect=OSError(EIO, "write failed"), autospec=True)

    with pytest.raises(OSError, match="write failed"):
        UnixFileLock(lock_path).acquire()

    assert lock_path.read_bytes() == b""
    with pytest.raises(Timeout):
        SoftFileLock(lock_path).acquire(timeout=0)


@NEEDS_FCNTL
@pytest.mark.filterwarnings("ignore:flock not supported on this filesystem:UserWarning")
@pytest.mark.usefixtures("unsupported_flock")
@NEEDS_STALE_RECLAIM
@pytest.mark.parametrize(
    ("make_lock", "umask"),
    [
        pytest.param(lambda path: UnixFileLock(path, mode=0o666), 0o022, id="explicit"),
        pytest.param(UnixFileLock, 0, id="umask"),
    ],
)
def test_fallback_creates_marker_with_restricted_writes(  # pragma: needs fcntl
    tmp_path: Path, make_lock: Callable[[Path], UnixFileLock], umask: int
) -> None:
    assert sys.platform != "win32"
    lock_path = tmp_path / "test.lock"
    previous_umask = os.umask(umask)
    try:
        lock = make_lock(lock_path)
        with lock:
            assert (lock.pid, stat.S_IMODE(lock_path.stat().st_mode)) == (os.getpid(), 0o644)
    finally:
        os.umask(previous_umask)


@NEEDS_FCNTL
@pytest.mark.usefixtures("unsupported_flock")
def test_fallback_restricts_writable_marker_without_exchange(  # pragma: needs fcntl
    tmp_path: Path, mocker: MockerFixture
) -> None:
    assert sys.platform != "win32"
    mocker.patch("filelock._native._unix.exchange_supported", autospec=True, return_value=False)
    lock_path = tmp_path / "test.lock"
    with pytest.warns(UserWarning, match="falling back"), UnixFileLock(lock_path, mode=0o666) as lock:
        assert (lock.pid, stat.S_IMODE(lock_path.stat().st_mode)) == (os.getpid(), 0o644)
    assert list(tmp_path.iterdir()) == []


@NEEDS_FCNTL
@NEEDS_STALE_RECLAIM
@pytest.mark.usefixtures("group_writable_umask")
@pytest.mark.filterwarnings("ignore:flock not supported on this filesystem:UserWarning")
def test_fallback_closes_the_displaced_native_descriptor(  # pragma: needs fcntl
    tmp_path: Path, mocker: MockerFixture
) -> None:
    # A mount can report different flock support for the private probe and the public file.
    assert sys.platform != "win32"
    flock = mocker.patch(
        "filelock._native._unix.fcntl.flock", autospec=True, side_effect=[None, OSError(ENOSYS, "no flock")]
    )

    with UnixFileLock(tmp_path / "test.lock", mode=0o666) as lock:
        assert lock.is_lock_held_by_us
        with pytest.raises(OSError, match=os.strerror(EBADF)) as closed:
            os.fstat(flock.call_args.args[0])
        assert closed.value.errno == EBADF
    assert list(tmp_path.iterdir()) == []


@NEEDS_FCNTL
@pytest.mark.filterwarnings("ignore:flock not supported on this filesystem:UserWarning")
@pytest.mark.usefixtures("unsupported_flock")
def test_fallback_keeps_peer_soft_marker(tmp_path: Path) -> None:  # pragma: needs fcntl
    lock_path = tmp_path / "test.lock"
    holder = UnixFileLock(lock_path)
    holder.acquire()
    marker = lock_path.stat().st_ino, lock_path.read_bytes()

    with pytest.raises(Timeout):
        UnixFileLock(lock_path).acquire(timeout=0)
    assert (lock_path.stat().st_ino, lock_path.read_bytes()) == marker
    holder.release()


@NEEDS_FCNTL
@pytest.mark.filterwarnings("ignore:flock not supported on this filesystem:UserWarning")
def test_fallback_keeps_the_created_marker_without_unlinking(  # pragma: needs fcntl
    tmp_path: Path, mocker: MockerFixture
) -> None:
    lock_path = tmp_path / "test.lock"
    peer = SoftFileLock(lock_path)
    unlink = mocker.spy(os, "unlink")
    mocker.patch("filelock._native._unix.fcntl.flock", side_effect=OSError(ENOSYS, "no flock"), autospec=True)
    lock = UnixFileLock(lock_path)
    try:
        lock.acquire(timeout=1)
        assert str(lock_path) not in [os.fsdecode(call.args[0]) for call in unlink.call_args_list]
        with pytest.raises(Timeout):
            peer.acquire(timeout=0)
    finally:
        mocker.stopall()
        lock.release()
        peer.release()


@NEEDS_FCNTL
@pytest.mark.filterwarnings("ignore:flock not supported on this filesystem:UserWarning")
@pytest.mark.usefixtures("unsupported_flock")
def test_fallback_retries_if_peer_replaces_unwritten_marker(  # pragma: needs fcntl
    tmp_path: Path, mocker: MockerFixture
) -> None:
    lock_path = tmp_path / "test.lock"
    peer = SoftFileLock(lock_path)
    write = os.write
    replaced: list[bool] = []

    def write_after_peer_replaces_marker(fd: int, data: bytes) -> int:
        if not replaced:
            replaced.append(True)
            lock_path.unlink()
            peer.acquire(blocking=False)
        return write(fd, data)

    mocker.patch("os.write", side_effect=write_after_peer_replaces_marker, autospec=True)
    lock = UnixFileLock(lock_path)
    try:
        with pytest.raises(Timeout):
            lock.acquire(timeout=0)
        assert peer.is_locked
        assert not lock.is_locked
    finally:
        mocker.stopall()
        lock.release()
        peer.release()


@NEEDS_FCNTL
@pytest.mark.filterwarnings("ignore:flock not supported on this filesystem:UserWarning")
@pytest.mark.usefixtures("unsupported_flock")
def test_fallback_retries_if_created_marker_disappears(  # pragma: needs fcntl
    tmp_path: Path, mocker: MockerFixture
) -> None:
    lock_path = tmp_path / "test.lock"
    lstat = os.lstat
    vanished: list[bool] = []

    def disappear_before_publish(path: str, *, dir_fd: int | None = None) -> os.stat_result:
        marker = lstat(path, dir_fd=dir_fd)
        if os.fspath(path) == os.fspath(lock_path) and not vanished:
            vanished.append(True)
            lock_path.unlink()
            raise FileNotFoundError(path)
        return marker

    mocker.patch("os.lstat", side_effect=disappear_before_publish, autospec=True)
    lock = UnixFileLock(lock_path)
    try:
        lock.acquire(timeout=0)
        assert (vanished, lock.is_locked) == ([True], True)
        assert lock_path.read_bytes().startswith(f"{os.getpid()}\n".encode())
    finally:
        mocker.stopall()
        lock.release()


@NEEDS_FCNTL
@pytest.mark.filterwarnings("ignore:flock not supported on this filesystem:UserWarning")
@pytest.mark.usefixtures("unsupported_flock")
def test_fallback_lstat_error_closes_pending_descriptor(
    tmp_path: Path, mocker: MockerFixture
) -> None:  # pragma: needs fcntl
    lock_path = tmp_path / "test.lock"
    mocker.patch("os.lstat", side_effect=OSError(EIO, "lstat failed"), autospec=True)
    lock = UnixFileLock(lock_path, mode=0o600)

    with pytest.raises(OSError, match="lstat failed"):
        lock.acquire(timeout=0)

    assert not lock.is_locked
    assert lock_path.read_bytes() == b""
