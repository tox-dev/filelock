from __future__ import annotations

import os
from typing import TYPE_CHECKING

import pytest

from filelock._util import break_lock_file

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
