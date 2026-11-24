from __future__ import annotations

import os
import shutil
import stat
import sys
from contextlib import closing
from pathlib import Path
from typing import TYPE_CHECKING, Final

import pytest
from capabilities import CAPABILITIES

from filelock import SoftReadWriteLock, Timeout
from filelock._soft._read_write import _storage as storage_mod
from tests.capability_marks import (
    NEEDS_FILE_MODE,
    NEEDS_FOREIGN_STICKY_TMP,
    NEEDS_USER_NAMESPACES,
)

from .lock_helpers import generations, holders

if TYPE_CHECKING:
    from collections.abc import Callable

    from pytest_mock import MockerFixture

pytestmark: Final[list[pytest.MarkDecorator]] = [
    pytest.mark.requires_hard_links,
    pytest.mark.usefixtures("clear_singletons"),
]


@pytest.mark.skipif(not CAPABILITIES["symlink"], reason="staging the protocol directory as a symlink")
def test_symlinked_protocol_directory_is_refused(
    lock_file: str, tmp_path: Path, lock: SoftReadWriteLock
) -> None:  # pragma: needs symlink
    victim_dir = tmp_path / "victim_dir"
    victim_dir.mkdir()
    Path(f"{lock_file}.rw").symlink_to(victim_dir)
    with pytest.raises(RuntimeError, match="not a directory or is a symlink"):
        lock.acquire_read(timeout=0.5)
    assert list(victim_dir.iterdir()) == []


@pytest.mark.skipif(
    not (CAPABILITIES["symlink"] and CAPABILITIES["link-dir-fd"]),
    reason="swapping in a symlinked directory is refused only where records are reached through descriptors",
)
@pytest.mark.parametrize("swapped", ["gen", "holders"])
def test_protocol_tree_swapped_after_an_acquire_is_refused(  # pragma: needs link-dir-fd
    lock_file: str, tmp_path: Path, lock: SoftReadWriteLock, swapped: str
) -> None:
    with lock.write_lock(timeout=2):
        pass
    (root := Path(f"{lock_file}.rw")).rename(tmp_path / "aside")
    for name in ("gen", "holders"):
        (root / name).mkdir(parents=True)
    (root / swapped).rmdir()
    (root / swapped).symlink_to(target := tmp_path / "target", target_is_directory=True)
    target.mkdir()

    with pytest.raises(RuntimeError, match="is not a directory or is a symlink"):
        lock.acquire_write(timeout=0.5)
    assert list(target.iterdir()) == []


@pytest.mark.skipif(sys.platform != "win32", reason="only Windows reaches records by name, through junctions")
@pytest.mark.parametrize("swapped", ["gen", "holders"])
def test_protocol_tree_with_a_junction_is_refused_before_first_acquire(  # pragma: win32 cover
    lock_file: str, tmp_path: Path, lock: SoftReadWriteLock, create_junction: Callable[[Path, Path], None], swapped: str
) -> None:
    _junction_the_tree(Path(f"{lock_file}.rw"), swapped, target := tmp_path / "target", create_junction)

    with pytest.raises(RuntimeError, match="is not a directory or is a symlink"):
        lock.acquire_write(timeout=0.5)
    assert list(target.iterdir()) == []


def _junction_the_tree(  # pragma: win32 cover
    root: Path, swapped: str, target: Path, create_junction: Callable[[Path, Path], None]
) -> None:
    for name in {"gen", "holders"} - {swapped}:
        (root / name).mkdir(parents=True)
    target.mkdir()
    create_junction(root / swapped, target)


@pytest.mark.skipif(sys.platform != "win32", reason="only Windows reaches records by name, through junctions")
@pytest.mark.parametrize("swapped", ["gen", "holders"])
def test_protocol_tree_with_a_junction_is_refused_after_first_acquire(  # pragma: win32 cover
    lock_file: str, tmp_path: Path, create_junction: Callable[[Path, Path], None], swapped: str
) -> None:
    with closing(SoftReadWriteLock(lock_file, is_singleton=False)) as lock:
        with lock.write_lock(timeout=2):
            pass
        root = Path(f"{lock_file}.rw")
        root.rename(tmp_path / "aside")
        _junction_the_tree(root, swapped, target := tmp_path / "target", create_junction)

        with pytest.raises(RuntimeError, match="is not a directory or is a symlink"):
            lock.acquire_write(timeout=0.5)
        assert list(target.iterdir()) == []


@pytest.mark.skipif(sys.platform != "win32", reason="only Windows reaches records by name")
def test_protocol_tree_stays_in_place_while_a_record_is_written(  # pragma: win32 cover
    lock_file: str, tmp_path: Path, lock: SoftReadWriteLock, mocker: MockerFixture
) -> None:
    real_write = storage_mod.write_all
    refused: list[bool] = []

    def try_to_move_the_tree_aside(fd: int, data: bytes) -> None:
        try:
            Path(f"{lock_file}.rw").rename(tmp_path / "aside")
        except PermissionError:
            refused.append(True)
        real_write(fd, data)

    mocker.patch.object(
        storage_mod,
        "write_all",
        new=mocker.create_autospec(storage_mod.write_all, side_effect=try_to_move_the_tree_aside),
    )
    with lock.write_lock(timeout=2):
        pass

    assert (bool(refused), (tmp_path / "aside").exists()) == (True, False)


@NEEDS_USER_NAMESPACES  # pragma: win32 no cover  # pragma: darwin no cover
@pytest.mark.skipif(not CAPABILITIES["link-dir-fd"], reason="checking the mount ID needs descriptor-based storage")
def test_write_lock_with_mapped_overflow_uid(sticky_dir_with_mapped_owner: Path) -> None:
    path = sticky_dir_with_mapped_owner / "marker"
    with (
        closing(SoftReadWriteLock(path, is_singleton=False)) as lock,
        lock.write_lock(timeout=1),
        closing(SoftReadWriteLock(path, is_singleton=False)) as peer,
        pytest.raises(Timeout),
    ):
        peer.acquire_write(timeout=0)


@NEEDS_FOREIGN_STICKY_TMP
@pytest.mark.usefixtures("directories_of_another_account")
def test_protocol_directory_of_another_account_is_refused(  # pragma: needs foreign-sticky-tmp
    lock_path_in_shared_tmp: Path,
) -> None:
    (root := Path(f"{lock_path_in_shared_tmp}.rw")).mkdir()
    lock = SoftReadWriteLock(lock_path_in_shared_tmp, is_singleton=False)
    try:
        with pytest.raises(RuntimeError, match="not to this process's account"):
            lock.acquire_write(timeout=0)
    finally:
        lock.close()
    assert list(root.iterdir()) == []


@NEEDS_FOREIGN_STICKY_TMP
@NEEDS_USER_NAMESPACES  # pragma: darwin no cover
@pytest.mark.usefixtures("uid_shared_with_unmapped_accounts")
def test_protocol_directory_with_an_unmapped_owner_is_refused(  # pragma: needs foreign-sticky-tmp
    lock_path_in_shared_tmp: Path,
) -> None:
    (root := Path(f"{lock_path_in_shared_tmp}.rw")).mkdir()
    lock = SoftReadWriteLock(lock_path_in_shared_tmp, is_singleton=False)
    try:
        with pytest.raises(RuntimeError, match="not to this process's account"):
            lock.acquire_write(timeout=0)
    finally:
        lock.close()
    assert list(root.iterdir()) == []


@NEEDS_FOREIGN_STICKY_TMP
def test_live_lock_refuses_a_tree_another_account_planted_after_a_recovery(  # pragma: needs foreign-sticky-tmp
    lock_path_in_shared_tmp: Path, mocker: MockerFixture
) -> None:
    lock = SoftReadWriteLock(lock_path_in_shared_tmp, is_singleton=False)
    try:
        with lock.write_lock(timeout=2):
            pass
        (root := Path(f"{lock_path_in_shared_tmp}.rw")).rename(aside := Path(f"{lock_path_in_shared_tmp}.rw.aside"))
        try:
            for name in ("gen", "holders"):
                (root / name).mkdir(parents=True)
            assert sys.platform != "win32"  # the capability is absent there; narrows os.geteuid for the type checker
            mocker.patch("filelock._filesystem._files.os.geteuid", autospec=True, return_value=os.geteuid() + 1)
            with pytest.raises(RuntimeError, match="not to this process's account"):
                lock.acquire_write(timeout=0.5)
            assert list((root / "holders").iterdir()) == []
        finally:
            shutil.rmtree(aside, ignore_errors=True)
    finally:
        lock.close()


@pytest.mark.usefixtures("directory_access")
def test_protocol_path_as_regular_file_is_refused(lock_file: str, lock: SoftReadWriteLock) -> None:
    Path(f"{lock_file}.rw").write_bytes(b"x")
    with pytest.raises(RuntimeError, match="not a directory or is a symlink"):
        lock.acquire_read(timeout=0.5)


@NEEDS_FILE_MODE
def test_records_are_owner_only(lock_file: str, lock: SoftReadWriteLock) -> None:  # pragma: needs file-mode
    with lock.write_lock(timeout=2):
        root = Path(f"{lock_file}.rw")
        for directory in (root, root / "gen", root / "holders"):
            assert stat.S_IMODE(directory.lstat().st_mode) == 0o700
        generation = root / "gen" / generations(lock_file)[-1]
        assert stat.S_IMODE(generation.lstat().st_mode) == 0o600
        (holder,) = holders(lock_file)
        assert stat.S_IMODE((root / "holders" / holder).lstat().st_mode) == 0o600


def test_stray_files_in_the_protocol_directories_are_ignored(lock_file: str, lock: SoftReadWriteLock) -> None:
    root = Path(f"{lock_file}.rw")
    (root / "gen").mkdir(parents=True)
    (root / "holders").mkdir()
    (root / "gen" / ".hidden").write_bytes(b"ignored")
    (root / "gen" / "not-a-generation").write_bytes(b"ignored")
    (root / "holders" / "not-a-token").write_bytes(b"ignored")
    with lock.write_lock(timeout=2):
        assert lock.generation is not None


def test_record_zero_write_rolls_back(lock_file: str, mocker: MockerFixture) -> None:
    mocker.patch("filelock._filesystem._files.os.write", return_value=0, autospec=True)

    lock = SoftReadWriteLock(lock_file, is_singleton=False)
    with pytest.raises(OSError, match="0 bytes"):
        lock.acquire_write(timeout=1)
    assert holders(lock_file) == []
