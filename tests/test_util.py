from __future__ import annotations

import os
import socket
import stat
import sys
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING, Final

import pytest

from filelock import SoftFileLock, Timeout
from filelock._util import break_lock_file, raise_on_not_writable_file

if TYPE_CHECKING:
    from collections.abc import Generator

    from pytest_mock import MockerFixture

_SKIP_AS_ROOT: Final[pytest.MarkDecorator] = pytest.mark.skipif(
    sys.platform != "win32" and os.geteuid() == 0,
    reason="root can write a 0o444 file",
)


def test_break_lock_file_unlinks_unchanged_file(tmp_path: Path) -> None:
    lock = tmp_path / "test.lock"
    lock.write_text("stale", encoding="utf-8")
    st = os.lstat(lock)
    break_lock_file(str(lock), st.st_mtime, st.st_ino)
    assert not lock.exists()
    assert list(tmp_path.glob("test.lock.break.*")) == []


def test_break_lock_file_preserves_file_when_mtime_advanced(tmp_path: Path) -> None:
    lock = tmp_path / "test.lock"
    lock.write_text("live", encoding="utf-8")
    # An mtime_before older than the file's real mtime models a peer recreating the lock after our stale read.
    # break_lock_file renames the live file aside but must not unlink it, so we never end with two live holders.
    break_lock_file(str(lock), mtime_before=0.0, ino_before=os.lstat(lock).st_ino)
    assert not lock.exists()
    leftover = list(tmp_path.glob("test.lock.break.*"))
    assert len(leftover) == 1
    assert leftover[0].read_text(encoding="utf-8") == "live"


def test_break_lock_file_preserves_file_when_inode_changed(tmp_path: Path) -> None:
    lock = tmp_path / "test.lock"
    lock.write_text("stale", encoding="utf-8")
    st = os.lstat(lock)
    # Model a coarse-granularity filesystem (NFS, FAT) where a peer broke and recreated the lock with a new inode
    # but the same mtime second. Creating the replacement while the original still exists guarantees a fresh inode.
    other = tmp_path / "recreated"
    other.write_text("live", encoding="utf-8")
    os.utime(other, ns=(st.st_atime_ns, st.st_mtime_ns))
    assert os.lstat(other).st_ino != st.st_ino
    other.replace(lock)
    break_lock_file(str(lock), st.st_mtime, st.st_ino)
    leftover = list(tmp_path.glob("test.lock.break.*"))
    assert len(leftover) == 1
    assert leftover[0].read_text(encoding="utf-8") == "live"


def test_break_lock_file_aborts_if_break_path_vanishes(tmp_path: Path, mocker: MockerFixture) -> None:
    lock = tmp_path / "test.lock"
    lock.write_text("x", encoding="utf-8")
    ino = os.lstat(lock).st_ino
    mocker.patch("filelock._util.os.lstat", side_effect=FileNotFoundError)
    break_lock_file(str(lock), 0.0, ino)
    assert not lock.exists()
    assert len(list(tmp_path.glob("test.lock.break.*"))) == 1


def test_break_lock_file_missing_source_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        break_lock_file(str(tmp_path / "nope.lock"), 0.0, 0)


def test_break_lock_file_break_path_not_targetable_by_a_peer(tmp_path: Path, mocker: MockerFixture) -> None:
    lock = tmp_path / "test.lock"
    lock.write_text("stale", encoding="utf-8")
    st = os.lstat(lock)

    # A second breaker in the same process independently computes this name (no random token). If break_lock_file
    # used it too, the peer could rename a freshly recreated live lock onto our break path in the window between the
    # re-verify lstat and the unlink, and we would delete a live lock the inode check just approved.
    predictable = tmp_path / f"test.lock.break.{os.getpid()}"
    real_lstat = os.lstat

    def lstat_hook(path: str) -> os.stat_result:
        result = real_lstat(path)
        # break_lock_file lstats the break path exactly once, so this guard only ever runs its body (once).
        if ".break." in path and not predictable.exists():  # pragma: no branch  # the peer recreates a live lock
            lock.write_text("live", encoding="utf-8")
            lock.rename(predictable)
        return result

    mocker.patch("filelock._util.os.lstat", side_effect=lstat_hook)
    break_lock_file(str(lock), st.st_mtime, st.st_ino)

    assert predictable.read_text(encoding="utf-8") == "live"


@pytest.mark.skipif(sys.platform == "win32", reason="symlink-to-dir raises IsADirectoryError only on Unix")
def test_raise_on_not_writable_file_does_not_follow_symlink_to_dir(tmp_path: Path) -> None:  # pragma: win32 no cover
    target = tmp_path / "targetdir"
    target.mkdir()
    link = tmp_path / "my.lock"
    link.symlink_to(target)
    # Following the symlink would see a directory and raise IsADirectoryError; lstat sees the link itself.
    raise_on_not_writable_file(str(link))
    assert stat.S_ISLNK(os.lstat(link).st_mode)


@pytest.mark.skipif(sys.platform == "win32", reason="symlink + 0o444 semantics differ on Windows")
@_SKIP_AS_ROOT  # pragma: win32 no cover
def test_raise_on_not_writable_file_does_not_follow_symlink_to_readonly(tmp_path: Path) -> None:
    target = tmp_path / "readonly"
    target.write_text("x", encoding="utf-8")
    target.chmod(0o444)
    link = tmp_path / "my.lock"
    link.symlink_to(target)
    # Following the symlink would see a read-only file and raise PermissionError; the link itself is writable.
    raise_on_not_writable_file(str(link))


@pytest.mark.skipif(sys.platform == "win32", reason="real dir raises PermissionError on Windows")
def test_raise_on_not_writable_file_still_rejects_real_directory(tmp_path: Path) -> None:  # pragma: win32 no cover
    path = tmp_path / "a_dir"
    path.mkdir()
    with pytest.raises(IsADirectoryError):  # pragma: win32 no cover
        raise_on_not_writable_file(str(path))


@pytest.mark.skipif(sys.platform == "win32", reason="Windows does not have read only files in the same way")
@_SKIP_AS_ROOT
def test_raise_on_not_writable_file_still_rejects_readonly_file(tmp_path: Path) -> None:  # pragma: win32 no cover
    path = tmp_path / "ro.lock"
    path.write_text("x", encoding="utf-8")
    path.chmod(0o444)
    try:  # pragma: win32 no cover
        with pytest.raises(PermissionError):  # pragma: win32 no cover
            raise_on_not_writable_file(str(path))
    finally:
        path.chmod(0o644)


@_SKIP_AS_ROOT
@pytest.mark.parametrize("mtime", [0, 2_000_000_000], ids=["mtime-zero", "mtime-future"])
def test_raise_on_not_writable_file_rejects_readonly_file_any_mtime(tmp_path: Path, mtime: int) -> None:
    path = tmp_path / "ro.lock"
    path.write_text("x", encoding="utf-8")
    path.chmod(0o444)
    try:
        os.utime(path, (mtime, mtime))
        with pytest.raises(PermissionError):
            raise_on_not_writable_file(str(path))
    finally:
        path.chmod(0o644)


@pytest.mark.skipif(sys.platform == "win32", reason="a real directory raises PermissionError on Windows")
def test_raise_on_not_writable_file_rejects_directory_with_mtime_zero(tmp_path: Path) -> None:  # pragma: win32 no cover
    path = tmp_path / "a_dir"
    path.mkdir()
    os.utime(path, (0, 0))
    with pytest.raises(IsADirectoryError):  # pragma: win32 no cover
        raise_on_not_writable_file(str(path))


@pytest.mark.parametrize(
    "mode", [pytest.param(0o444, id="acl"), pytest.param(0o464, id="group"), pytest.param(0o446, id="other")]
)
@pytest.mark.parametrize("effective_ids", [pytest.param(True, id="supported"), pytest.param(False, id="unsupported")])
def test_writability_uses_open_credentials(
    readonly_marker: Path, mocker: MockerFixture, mode: int, *, effective_ids: bool
) -> None:
    readonly_marker.chmod(mode)

    def access(filename: str, permission: int, *, effective_ids: bool = False) -> bool:
        assert (filename, permission) == (str(readonly_marker), os.W_OK)
        return effective_ids == supported

    supported: Final = effective_ids
    probe: Final = mocker.patch("os.access", autospec=True, side_effect=access)
    mocker.patch("os.supports_effective_ids", {probe} if effective_ids else set())
    with pytest.raises(Timeout):
        SoftFileLock(readonly_marker).acquire(timeout=0)


def test_writability_allows_creation_after_marker_disappears(readonly_marker: Path, mocker: MockerFixture) -> None:
    def access(filename: str, permission: int, *, effective_ids: bool = False) -> bool:
        assert (permission, effective_ids) == (os.W_OK, os.access in os.supports_effective_ids)
        readonly_marker.chmod(0o644)
        Path(filename).unlink()
        return False

    mocker.patch("os.access", autospec=True, side_effect=access)
    with (lock := SoftFileLock(readonly_marker)).acquire(timeout=0):
        assert lock.is_locked


def test_writability_rejects_denied_marker(readonly_marker: Path, mocker: MockerFixture) -> None:
    mocker.patch("os.access", autospec=True, return_value=False)
    with pytest.raises(PermissionError):
        SoftFileLock(readonly_marker).acquire(timeout=0)


@pytest.fixture
def readonly_marker(tmp_path: Path) -> Generator[Path]:
    path: Final = tmp_path / "test.lock"
    path.write_text(f"424242\n{socket.gethostname()}-other\n", encoding="utf-8")
    path.chmod(0o444)
    try:
        yield path
    finally:
        with suppress(FileNotFoundError):
            path.chmod(0o644)
