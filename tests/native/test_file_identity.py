from __future__ import annotations

import os
import sys
import threading
from errno import EAGAIN, ENOSYS, EWOULDBLOCK
from typing import TYPE_CHECKING

import pytest

from filelock import (
    BaseFileLock,
    FileLock,
    SoftFileLock,
)
from tests.capability_marks import (
    NEEDS_FCNTL,
    WINDOWS_ONLY,
)

if TYPE_CHECKING:
    from pathlib import Path

    from pytest_mock import MockerFixture


@pytest.mark.skipif(sys.platform != "win32" and os.geteuid() == 0, reason="root can open a 0o444 file for writing")
@pytest.mark.parametrize("lock_type", [SoftFileLock, FileLock])
def test_readonly_lock_file_with_mtime_zero_raises(lock_type: type[BaseFileLock], tmp_path: Path) -> None:
    lock_path = tmp_path / "z.lock"
    lock_path.touch()
    lock_path.chmod(0o444)
    os.utime(lock_path, (0, 0))
    try:
        with pytest.raises(PermissionError):
            lock_type(str(lock_path)).acquire(timeout=0)
    finally:
        lock_path.chmod(0o644)


@pytest.mark.parametrize("lock_type", [SoftFileLock])
def test_lock_file_removed_after_release(tmp_path: Path, lock_type: type[BaseFileLock]) -> None:
    lock_path = tmp_path / "test.lock"
    with lock_type(str(lock_path)):
        assert lock_path.exists()
    assert not lock_path.exists()


@NEEDS_FCNTL  # pragma: needs fcntl
def test_concurrent_acquire_release_keeps_lock_file(tmp_path: Path) -> None:
    lock_path = tmp_path / "test.lock"
    errors: list[Exception] = []

    def worker() -> None:
        try:
            for _ in range(20):
                with FileLock(str(lock_path), is_singleton=False):
                    pass
        except Exception as exc:  # pragma: no cover  # a healthy run never raises here
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert not errors, errors
    assert lock_path.exists()


@NEEDS_FCNTL  # pragma: needs fcntl
def test_lock_acquired_after_release_keeps_path(tmp_path: Path) -> None:
    lock_path = tmp_path / "test.lock"
    first = FileLock(str(lock_path), is_singleton=False)
    second = FileLock(str(lock_path), is_singleton=False)

    first.acquire()
    assert lock_path.exists()
    first.release()
    assert lock_path.exists()

    second.acquire()
    assert lock_path.exists()
    second.release()
    assert lock_path.exists()


@NEEDS_FCNTL  # pragma: needs fcntl
def test_waiter_fd_cannot_split_lock_after_release(tmp_path: Path) -> None:
    # typeshed exposes fcntl only on POSIX.
    assert sys.platform != "win32"
    import fcntl

    lock_path = tmp_path / "test.lock"
    first = FileLock(str(lock_path), is_singleton=False)
    replacement = FileLock(str(lock_path), is_singleton=False)

    first.acquire()
    waiter_fd = os.open(lock_path, os.O_RDWR)

    try:
        first.release()
        assert lock_path.exists()

        replacement.acquire()
        try:
            with pytest.raises(BlockingIOError) as exc_info:
                fcntl.flock(waiter_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            assert exc_info.value.errno in {EAGAIN, EWOULDBLOCK}
        finally:
            replacement.release()
    finally:
        os.close(waiter_fd)


def test_preserve_lock_file_defaults_off(tmp_path: Path) -> None:
    assert FileLock(str(tmp_path / "a")).preserve_lock_file is False


def test_preserve_lock_file_property_reflects_argument(tmp_path: Path) -> None:
    assert FileLock(str(tmp_path / "a"), preserve_lock_file=True).preserve_lock_file is True


def test_preserve_lock_file_rejected_by_soft_lock(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="preserve_lock_file"):
        SoftFileLock(str(tmp_path / "a"), preserve_lock_file=True)


def test_singleton_rejects_different_preserve_lock_file(tmp_path: Path) -> None:
    path = str(tmp_path / "a")
    first = FileLock(path, is_singleton=True, preserve_lock_file=False)
    try:
        with pytest.raises(ValueError, match="preserve_lock_file"):
            FileLock(path, is_singleton=True, preserve_lock_file=True)
    finally:
        first.release(force=True)


@NEEDS_FCNTL  # pragma: needs fcntl
def test_preserve_lock_file_unix_keeps_pathname(tmp_path: Path) -> None:
    path = tmp_path / "a"
    lock = FileLock(str(path), preserve_lock_file=True)
    lock.acquire()
    lock.release()
    assert path.exists()
    assert type(lock).__name__ == "UnixFileLock"


@NEEDS_FCNTL  # pragma: needs fcntl
def test_preserve_lock_file_fails_closed_on_enosys(tmp_path: Path, mocker: MockerFixture) -> None:
    mocker.patch("filelock._native._unix.fcntl.flock", side_effect=OSError(ENOSYS, "no flock"), autospec=True)
    lock = FileLock(str(tmp_path / "a"), preserve_lock_file=True)
    with pytest.raises(OSError, match="no flock"):
        lock.acquire()
    assert not lock.is_locked
    assert type(lock).__name__ == "UnixFileLock"


@WINDOWS_ONLY  # pragma: win32 cover
def test_preserve_lock_file_windows_default_removes_file(tmp_path: Path) -> None:
    path = tmp_path / "a"
    lock = FileLock(str(path))
    lock.acquire()
    lock.release()
    assert not path.exists()


@WINDOWS_ONLY  # pragma: win32 cover
def test_preserve_lock_file_windows_keeps_file(tmp_path: Path) -> None:
    path = tmp_path / "a"
    lock = FileLock(str(path), preserve_lock_file=True)
    lock.acquire()
    lock.release()
    assert path.exists()


@WINDOWS_ONLY  # pragma: win32 cover
def test_preserve_lock_file_windows_reacquires_same_identity(tmp_path: Path) -> None:
    path = tmp_path / "a"
    lock = FileLock(str(path), preserve_lock_file=True)
    lock.acquire()
    lock.release()
    kept = path.stat()
    lock.acquire()
    try:
        current = path.stat()
        assert (current.st_dev, current.st_ino) == (kept.st_dev, kept.st_ino)
    finally:
        lock.release()


@WINDOWS_ONLY  # pragma: win32 cover
def test_preserve_lock_file_windows_kept_after_forced_release(tmp_path: Path) -> None:
    path = tmp_path / "a"
    lock = FileLock(str(path), preserve_lock_file=True)
    lock.acquire()
    lock.acquire()
    lock.release(force=True)
    assert not lock.is_locked
    assert path.exists()
