from __future__ import annotations

import os
import stat
import sys
from typing import TYPE_CHECKING

import pytest

from filelock._util import break_lock_file, raise_on_not_writable_file

if TYPE_CHECKING:
    from pathlib import Path

    from pytest_mock import MockerFixture


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
    # A mtime_before older than the file's real mtime models a peer recreating the lock after our stale read: the
    # live file is renamed aside but must not be unlinked, so the holder's content survives instead of two holders.
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
        if ".break." in path and not predictable.exists():  # once: the peer recreates a live lock at its own name
            lock.write_text("live", encoding="utf-8")
            lock.rename(predictable)
        return result

    mocker.patch("filelock._util.os.lstat", side_effect=lstat_hook)
    break_lock_file(str(lock), st.st_mtime, st.st_ino)

    assert predictable.read_text(encoding="utf-8") == "live"


@pytest.mark.skipif(sys.platform == "win32", reason="symlink-to-dir raises IsADirectoryError only on Unix")
def test_raise_on_not_writable_file_does_not_follow_symlink_to_dir(tmp_path: Path) -> None:
    target = tmp_path / "targetdir"
    target.mkdir()
    link = tmp_path / "my.lock"
    link.symlink_to(target)
    # Following the symlink would see a directory and raise IsADirectoryError; lstat sees the link itself.
    raise_on_not_writable_file(str(link))
    assert stat.S_ISLNK(os.lstat(link).st_mode)


@pytest.mark.skipif(sys.platform == "win32", reason="symlink + 0o444 semantics differ on Windows")
@pytest.mark.skipif(
    sys.platform != "win32" and os.geteuid() == 0,
    reason="root can write a 0o444 file, so following the symlink would not raise",
)
def test_raise_on_not_writable_file_does_not_follow_symlink_to_readonly(tmp_path: Path) -> None:
    target = tmp_path / "readonly"
    target.write_text("x", encoding="utf-8")
    target.chmod(0o444)
    link = tmp_path / "my.lock"
    link.symlink_to(target)
    # Following the symlink would see a read-only file and raise PermissionError; the link itself is writable.
    raise_on_not_writable_file(str(link))


@pytest.mark.skipif(sys.platform == "win32", reason="real dir raises PermissionError on Windows")
def test_raise_on_not_writable_file_still_rejects_real_directory(tmp_path: Path) -> None:
    path = tmp_path / "a_dir"
    path.mkdir()
    with pytest.raises(IsADirectoryError):
        raise_on_not_writable_file(str(path))


@pytest.mark.skipif(sys.platform == "win32", reason="Windows does not have read only files in the same way")
@pytest.mark.skipif(
    sys.platform != "win32" and os.geteuid() == 0,
    reason="root can write a 0o444 file",
)
def test_raise_on_not_writable_file_still_rejects_readonly_file(tmp_path: Path) -> None:
    path = tmp_path / "ro.lock"
    path.write_text("x", encoding="utf-8")
    path.chmod(0o444)
    try:
        with pytest.raises(PermissionError):
            raise_on_not_writable_file(str(path))
    finally:
        path.chmod(0o644)
