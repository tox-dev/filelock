from __future__ import annotations

import os
from errno import EACCES, EIO
from typing import TYPE_CHECKING

import pytest

from filelock import Timeout, UnixFileLock
from tests.capability_marks import NEEDS_FCNTL

if TYPE_CHECKING:
    from pathlib import Path

    from pytest_mock import MockerFixture


@NEEDS_FCNTL
@pytest.mark.filterwarnings("ignore:flock not supported on this filesystem:UserWarning")
@pytest.mark.usefixtures("unsupported_flock", "write_denied_lock_file")
def test_fallback_waits_on_a_peer_marker_it_cannot_write(tmp_path: Path) -> None:  # pragma: needs fcntl
    lock_path = tmp_path / "test.lock"
    holder = UnixFileLock(lock_path)
    holder.acquire()

    with pytest.raises(Timeout):
        UnixFileLock(lock_path).acquire(timeout=0)
    holder.release()
    with (contender := UnixFileLock(lock_path)).acquire(timeout=0):
        assert contender.is_locked


@NEEDS_FCNTL
@pytest.mark.usefixtures("write_denied_lock_file")
def test_lock_file_it_cannot_write_stays_denied_where_flock_works(tmp_path: Path) -> None:  # pragma: needs fcntl
    (lock_path := tmp_path / "test.lock").touch()

    with pytest.raises(PermissionError):
        UnixFileLock(lock_path).acquire(timeout=0)


@NEEDS_FCNTL
def test_denied_lock_file_is_reopened_in_the_acquisition_directory(  # pragma: needs fcntl
    working_directories: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch, mocker: MockerFixture
) -> None:
    # A relative reopen after chdir would probe a different directory.
    first, second = working_directories
    (first / "test.lock").touch()
    real_open = os.open
    read_only_opens: list[str] = []

    def deny_write_then_chdir(path: str, flags: int, mode: int = 0o777, *, dir_fd: int | None = None) -> int:
        if not flags & os.O_CREAT and "test.lock" in path:
            if flags & os.O_RDWR:
                monkeypatch.chdir(second)
                raise PermissionError(EACCES, "Permission denied", path)
            read_only_opens.append(path)
        return real_open(path, flags, mode, dir_fd=dir_fd)

    mocker.patch("os.open", side_effect=deny_write_then_chdir, autospec=True)
    with pytest.raises(PermissionError):
        UnixFileLock("test.lock", mode=0o600).acquire(timeout=0)

    assert read_only_opens == [str(first.resolve() / "test.lock")]


@NEEDS_FCNTL
@pytest.mark.parametrize("write_denied_lock_file", ["leased"], indirect=True)
@pytest.mark.usefixtures("write_denied_lock_file")
def test_leased_lock_file_it_cannot_write_is_contention_where_flock_works(
    tmp_path: Path,
) -> None:  # pragma: needs fcntl
    (lock_path := tmp_path / "test.lock").touch()

    with pytest.raises(Timeout):
        UnixFileLock(lock_path).acquire(timeout=0)


@NEEDS_FCNTL
@pytest.mark.filterwarnings("ignore:flock not supported on this filesystem:UserWarning")
@pytest.mark.parametrize("write_denied_lock_file", ["leased"], indirect=True)
@pytest.mark.usefixtures("unsupported_flock", "write_denied_lock_file")
def test_fallback_treats_a_leased_marker_it_cannot_write_as_contention(tmp_path: Path) -> None:  # pragma: needs fcntl
    (lock_path := tmp_path / "test.lock").touch()

    with pytest.raises(Timeout):
        UnixFileLock(lock_path).acquire(timeout=0)


@NEEDS_FCNTL
@pytest.mark.parametrize(
    "write_denied_lock_file", [pytest.param("unlink", id="unlinked-under-the-denial")], indirect=True
)
@pytest.mark.usefixtures("write_denied_lock_file")
def test_lock_file_unlinked_under_a_denied_open_is_retried(tmp_path: Path) -> None:  # pragma: needs fcntl
    (lock_path := tmp_path / "test.lock").touch()

    with (lock := UnixFileLock(lock_path)).acquire(timeout=1):
        assert lock.is_locked


@NEEDS_FCNTL  # pragma: needs fcntl
def test_trailing_slash_path_raises_instead_of_retrying(tmp_path: Path) -> None:
    # macOS reports ENOENT for a trailing slash and Linux reports EISDIR. Both mean the path is invalid.
    with pytest.raises((FileNotFoundError, IsADirectoryError)):
        UnixFileLock(f"{tmp_path / 'missing'}{os.sep}").acquire(timeout=0)


@NEEDS_FCNTL
def test_release_suppresses_eio_on_close(tmp_path: Path, mocker: MockerFixture) -> None:  # pragma: needs fcntl
    lock = UnixFileLock(tmp_path / "test.lock")
    lock.acquire()

    real_close = os.close  # capture before the patch so the fd still closes for real
    fd_to_fail = lock._context.lock_file_fd  # _release() nulls this before closing, so read it now

    def _close_eio(fd: int) -> None:
        real_close(fd)
        if fd == fd_to_fail:  # pragma: no branch  # only the lock's own descriptor closes while the patch is live
            raise OSError(EIO, "Input/output error")

    mocker.patch("filelock._native._unix.os.close", side_effect=_close_eio, autospec=True)
    lock.release()
    assert not lock.is_locked


@NEEDS_FCNTL  # pragma: needs fcntl
def test_acquire_flock_error_clears_pending_descriptor(tmp_path: Path, mocker: MockerFixture) -> None:
    lock = UnixFileLock(tmp_path / "test.lock")
    failed_flock = mocker.patch(
        "filelock._native._unix.fcntl.flock", side_effect=OSError(EIO, "Input/output error"), autospec=True
    )

    with pytest.raises(OSError, match="Input/output error"):
        lock.acquire(timeout=0)
    mocker.stop(failed_flock)
    assert not lock.is_locked

    lock.acquire(timeout=0)
    assert lock.is_locked
    lock.release()
    assert not lock.is_locked
