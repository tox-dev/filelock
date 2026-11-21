from __future__ import annotations

import os
import signal
import sys
import time
from errno import EWOULDBLOCK
from types import SimpleNamespace
from typing import TYPE_CHECKING, Final

import pytest
from capabilities import CAPABILITIES

from filelock import (
    BaseFileLock,
    FileLock,
    StrictSoftFileLock,
    Timeout,
    UnixFileLock,
)
from tests.capability_marks import (
    NEEDS_FCNTL,
    NEEDS_FOREIGN_STICKY_TMP,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path

    from pytest_mock import MockerFixture


@NEEDS_FCNTL  # pragma: needs fcntl
def test_stale_inode_retry_on_unlinked_lock(tmp_path: Path, mocker: MockerFixture) -> None:
    lock_path = tmp_path / "test.lock"
    lock = FileLock(lock_path, mode=0o600, is_singleton=False)

    real_fstat = os.fstat
    call_count = 0

    def fstat_unlinked_once(fd: int) -> os.stat_result:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            lock_path.unlink()
        return real_fstat(fd)

    mocker.patch("os.fstat", side_effect=fstat_unlinked_once, autospec=True)
    lock.acquire()
    assert lock.is_locked
    assert call_count == 2
    lock.release()


@NEEDS_FCNTL  # pragma: needs fcntl
def test_permission_error_fallback_without_o_creat(tmp_path: Path, mocker: MockerFixture) -> None:
    lock_path = tmp_path / "test.lock"
    lock_path.touch()
    lock = FileLock(str(lock_path), is_singleton=False, mode=0o600)

    real_open = os.open
    call_count = 0

    def open_no_creat(path: str, flags: int, mode: int = 0o777, *, dir_fd: int | None = None) -> int:
        nonlocal call_count
        call_count += 1
        if call_count == 1 and flags & os.O_CREAT:
            raise PermissionError(13, "Permission denied", path)
        return real_open(path, flags, mode) if dir_fd is None else real_open(path, flags, mode, dir_fd=dir_fd)

    mocker.patch("os.open", side_effect=open_no_creat, autospec=True)
    lock.acquire()
    assert lock.is_locked
    assert call_count == 2
    lock.release()


@NEEDS_FCNTL  # pragma: needs fcntl
def test_permission_error_propagates_when_file_missing(tmp_path: Path, mocker: MockerFixture) -> None:
    lock = FileLock(str(tmp_path / "test.lock"), is_singleton=False)

    real_open = os.open

    def open_always_permission_error(path: str, flags: int, mode: int = 0o777, *, dir_fd: int | None = None) -> int:
        if "test.lock" in path:
            raise PermissionError(13, "Permission denied", path)
        return (  # pragma: no cover  # every os.open during this acquire targets the lock path
            real_open(path, flags, mode) if dir_fd is None else real_open(path, flags, mode, dir_fd=dir_fd)
        )

    mocker.patch("os.open", side_effect=open_always_permission_error, autospec=True)
    with pytest.raises(PermissionError, match="Permission denied"):
        lock.acquire(timeout=0)


@NEEDS_FCNTL
@NEEDS_FOREIGN_STICKY_TMP
@pytest.mark.usefixtures("directories_of_another_account")
def test_lock_file_of_another_account_under_sticky_parent_is_refused(  # pragma: needs foreign-sticky-tmp
    lock_path_in_shared_tmp: Path,
) -> None:
    # Its owner could unlink it during the hold, letting a second process lock a new file at the same path.
    lock_path_in_shared_tmp.touch()
    lock = FileLock(lock_path_in_shared_tmp, timeout=0, is_singleton=False)

    with pytest.raises(PermissionError, match="not to this process's account") as raised:
        lock.acquire()
    # The error names the path the lock resolved, and /tmp is a symlink to /private/tmp on macOS.
    expected = (str(lock_path_in_shared_tmp.resolve()), False, True)
    assert (raised.value.filename, lock.is_locked, lock_path_in_shared_tmp.exists()) == expected


@NEEDS_FCNTL  # pragma: needs fcntl
def test_callback_lock_retries_after_the_link_count_returns_to_one(tmp_path: Path, mocker: MockerFixture) -> None:
    tmp_path.chmod(0o1777)
    lock_path = tmp_path / "test.lock"
    lock_path.write_bytes(b"holder metadata")
    calls: list[int] = []
    lock = FileLock(lock_path, timeout=0, on_acquired=calls.append)
    # Some runtimes can inspect a file's link count but cannot create a second name for it.
    links = mocker.patch("os.fstat", autospec=True, return_value=SimpleNamespace(st_nlink=2))

    with pytest.raises(PermissionError, match="another hard link") as raised:
        lock.acquire()
    assert (raised.value.filename, lock.is_locked, calls) == (str(lock_path.resolve()), False, [])
    assert lock_path.read_bytes() == b"holder metadata"

    mocker.stop(links)
    with lock:
        (fd,) = calls
        assert os.path.samestat(os.fstat(fd), lock_path.stat())


@NEEDS_FCNTL  # pragma: needs fcntl
@pytest.mark.requires_hard_links  # pragma: needs hard-link
def test_hard_link_in_a_shared_directory_is_kept_from_the_callback(tmp_path: Path) -> None:
    tmp_path.chmod(0o1777)
    (victim := tmp_path / "victim").write_bytes(b"keep me")
    os.link(victim, lock_path := tmp_path / "test.lock")
    calls: list[int] = []
    lock = FileLock(lock_path, timeout=0, is_singleton=False, on_acquired=calls.append)
    with pytest.raises(PermissionError, match="another hard link") as raised:
        lock.acquire()
    assert (raised.value.filename, lock.is_locked, calls, victim.read_bytes()) == (
        str(lock_path.resolve()),
        False,
        [],
        b"keep me",
    )


@NEEDS_FCNTL  # pragma: needs fcntl
@pytest.mark.requires_hard_links  # pragma: needs hard-link
@pytest.mark.parametrize(
    ("directory_mode", "on_acquired"),
    [
        pytest.param(0o700, os.fsync, id="private-directory"),
        pytest.param(0o1777, None, id="no-callback"),
    ],
)
def test_hard_link_is_locked_where_no_other_account_can_misdirect_a_callback(
    tmp_path: Path, directory_mode: int, on_acquired: Callable[[int], None] | None
) -> None:
    tmp_path.chmod(directory_mode)
    (lock_path := tmp_path / "test.lock").touch()
    os.link(lock_path, tmp_path / "alias.lock")
    with FileLock(lock_path, timeout=0, is_singleton=False, on_acquired=on_acquired) as lock:
        assert lock.is_locked


@NEEDS_FCNTL  # pragma: needs fcntl
@pytest.mark.requires_hard_links  # pragma: needs hard-link
def test_hard_linked_names_share_a_native_lock_without_a_callback(tmp_path: Path) -> None:
    path = tmp_path / "test.lock"
    path.write_bytes(b"holder metadata")
    alias = tmp_path / "alias"
    alias.hardlink_to(path)
    contender = UnixFileLock(alias, timeout=0)

    with UnixFileLock(path), pytest.raises(Timeout):
        contender.acquire()
    with contender:
        assert alias.read_bytes() == b"holder metadata"


@pytest.mark.skipif(sys.platform not in {"linux", "android"}, reason="ownership proof uses Linux procfs")
@pytest.mark.parametrize(
    "lock_type",
    [
        pytest.param(UnixFileLock, marks=NEEDS_FCNTL, id="flock"),
        pytest.param(StrictSoftFileLock, marks=pytest.mark.requires_hard_links, id="strict"),
    ],
)
def test_lock_with_mapped_overflow_uid(  # pragma: win32 no cover  # pragma: darwin no cover
    lock_type: type[BaseFileLock], sticky_dir_with_mapped_owner: Path
) -> None:
    lock = lock_type(sticky_dir_with_mapped_owner / "marker", timeout=0)
    with lock:
        assert lock.is_locked


@NEEDS_FCNTL  # pragma: needs fcntl
def test_sticky_bit_fallback_handles_concurrent_unlink(tmp_path: Path, mocker: MockerFixture) -> None:
    lock_path = tmp_path / "test.lock"
    lock_path.touch()
    lock = FileLock(str(lock_path), is_singleton=False, mode=0o600)

    real_open = os.open
    denied_create = False
    unlinked_open = False

    # GraalPy can open unrelated paths mid-acquire. Match the flags to fail only the intended opens.
    def open_permission_then_unlink(path: str, flags: int, mode: int = 0o777, *, dir_fd: int | None = None) -> int:
        nonlocal denied_create, unlinked_open
        if not denied_create and flags & os.O_CREAT and "test.lock" in path:
            denied_create = True
            raise PermissionError(13, "Permission denied", path)
        if not unlinked_open and not flags & os.O_CREAT and "test.lock" in path:
            unlinked_open = True
            lock_path.unlink(missing_ok=True)
            raise FileNotFoundError(2, "No such file or directory", path)
        return real_open(path, flags, mode) if dir_fd is None else real_open(path, flags, mode, dir_fd=dir_fd)

    mocker.patch("os.open", side_effect=open_permission_then_unlink, autospec=True)
    lock.acquire()
    assert lock.is_locked
    assert (denied_create, unlinked_open) == (True, True)
    lock.release()


@NEEDS_FCNTL  # pragma: needs fcntl
def test_existing_lock_file_open_that_would_block_is_contention(tmp_path: Path, mocker: MockerFixture) -> None:
    lock_path = tmp_path / "test.lock"
    lock_path.touch()
    real_open = os.open

    def open_would_block(path: str, flags: int, mode: int = 0o777, *, dir_fd: int | None = None) -> int:
        if not flags & os.O_CREAT and "test.lock" in path:
            raise BlockingIOError(EWOULDBLOCK, "Resource temporarily unavailable", path)
        return real_open(path, flags, mode, dir_fd=dir_fd)

    mocker.patch("os.open", side_effect=open_would_block, autospec=True)
    with pytest.raises(Timeout):
        FileLock(str(lock_path), is_singleton=False, mode=0o600).acquire(timeout=0)


_SKIP_AS_ROOT: Final[pytest.MarkDecorator] = pytest.mark.skipif(
    sys.platform != "win32" and os.geteuid() == 0, reason="root can write a 0o400 file"
)

_NEEDS_FILE_LEASE: Final[pytest.MarkDecorator] = pytest.mark.skipif(
    not CAPABILITIES["file-lease"], reason="leasing the lock file needs Linux F_SETLEASE on a filesystem that grants it"
)


@pytest.fixture
def leased_lock_path(tmp_path: Path) -> Iterator[Path]:  # pragma: needs file-lease
    # typeshed exposes fcntl only on POSIX.
    assert sys.platform != "win32"
    import fcntl

    lock_path = tmp_path / "test.lock"
    # Ignoring SIGIO keeps the lease while the contender attempts to open the file.
    previous_handler = signal.signal(signal.SIGIO, signal.SIG_IGN)
    lease_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT)
    try:
        fcntl.fcntl(lease_fd, fcntl.F_SETLEASE, fcntl.F_WRLCK)
        yield lock_path
    finally:
        os.close(lease_fd)
        signal.signal(signal.SIGIO, previous_handler)


@_NEEDS_FILE_LEASE  # pragma: needs file-lease
@pytest.mark.parametrize(
    ("timeout", "blocking"), [pytest.param(0.1, True, id="timeout"), pytest.param(-1, False, id="non-blocking")]
)
def test_lease_on_lock_file_does_not_outlast_deadline(
    leased_lock_path: Path, timeout: float, *, blocking: bool
) -> None:
    started = time.monotonic()
    with pytest.raises(Timeout):
        FileLock(str(leased_lock_path), is_singleton=False).acquire(timeout=timeout, blocking=blocking)
    assert time.monotonic() - started < 5


@_NEEDS_FILE_LEASE  # pragma: needs file-lease
@_SKIP_AS_ROOT
def test_lease_on_a_read_only_lock_file_is_contention(leased_lock_path: Path) -> None:
    leased_lock_path.chmod(0o400)

    started = time.monotonic()
    with pytest.raises(Timeout):
        FileLock(str(leased_lock_path), is_singleton=False).acquire(timeout=0.1)
    assert time.monotonic() - started < 5


@NEEDS_FCNTL  # pragma: needs fcntl
def test_filenotfound_on_fuse_nfs_retries(tmp_path: Path, mocker: MockerFixture) -> None:
    """Retry recovers from the FUSE/NFS os.open(O_CREAT) race that raises FileNotFoundError."""
    lock = FileLock(str(tmp_path / "test.lock"), is_singleton=False, mode=0o600)

    real_open = os.open
    call_count = 0

    def open_enoent_then_succeed(path: str, flags: int, mode: int = 0o777, *, dir_fd: int | None = None) -> int:
        nonlocal call_count
        call_count += 1
        if call_count == 1 and flags & os.O_CREAT and "test.lock" in path:
            raise FileNotFoundError(2, "No such file or directory", path)
        return real_open(path, flags, mode) if dir_fd is None else real_open(path, flags, mode, dir_fd=dir_fd)

    mocker.patch("os.open", side_effect=open_enoent_then_succeed, autospec=True)
    lock.acquire()
    assert lock.is_locked
    assert call_count >= 2
    lock.release()
