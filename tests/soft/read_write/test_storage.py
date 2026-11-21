from __future__ import annotations

import os
import shutil
import sys
import time
from errno import EIO, ENXIO
from pathlib import Path
from typing import TYPE_CHECKING, Final

import pytest
from capabilities import CAPABILITIES

from filelock import SoftFileLockProtocolError
from filelock._soft._read_write import _storage as storage_mod
from filelock._soft._read_write._storage import OsFiles, TreeReplacedError
from tests.capability_marks import NEEDS_FOREIGN_STICKY_TMP

if TYPE_CHECKING:
    from collections.abc import Callable

    from pytest_mock import MockerFixture

pytestmark: Final[pytest.MarkDecorator] = pytest.mark.requires_hard_links


@pytest.mark.parametrize(
    ("call", "expected"),
    [
        pytest.param(lambda files, path: files.read(path), None, id="read"),
        pytest.param(lambda files, path: files.overwrite(path, b"data", (0, 0), b""), False, id="overwrite"),
        pytest.param(lambda files, path: files.listdir(path), [], id="listdir"),
    ],
)
def test_missing_path_returns_default(
    tmp_path: Path,
    call: Callable[[OsFiles, str], bytes | bool | list[str] | None],
    expected: bytes | bool | list[str] | None,
) -> None:
    assert call(OsFiles(str(tmp_path / "x.lock")), str(tmp_path / "absent")) == expected


@pytest.mark.parametrize(
    "call",
    [
        pytest.param(lambda files, path: files.read(path), id="read"),
        pytest.param(lambda files, path: files.overwrite(path, b"data", (0, 0), b""), id="overwrite"),
    ],
)
def test_open_other_errors_propagate(
    tmp_path: Path, mocker: MockerFixture, call: Callable[[OsFiles, str], object]
) -> None:
    mocker.patch.object(storage_mod.os, "open", side_effect=OSError(EIO, "Input/output error"), autospec=True)
    with pytest.raises(OSError, match="Input/output error"):
        call(OsFiles(str(tmp_path / "x.lock")), str(tmp_path / "absent"))


def test_read_non_regular_file_is_empty(tmp_path: Path) -> None:
    # The null device provides a non-regular file on every supported platform.
    assert OsFiles(str(tmp_path / "x.lock")).read(os.devnull) == b""


def test_create_rolls_back_a_failed_write(tmp_path: Path, mocker: MockerFixture) -> None:
    mocker.patch.object(
        storage_mod, "write_all", new=mocker.create_autospec(storage_mod.write_all, side_effect=OSError("write boom"))
    )
    with pytest.raises(OSError, match="write boom"):
        OsFiles(str(tmp_path / "x.lock")).create(str(tmp_path / "record"), b"data")
    assert not (tmp_path / "record").exists()


@pytest.mark.parametrize("durable", [pytest.param(True, id="durable"), pytest.param(False, id="holder")])
def test_create_flushes_only_a_durable_record(tmp_path: Path, mocker: MockerFixture, *, durable: bool) -> None:
    # An empty generation would survive a crash and block its successors.
    fsync = mocker.spy(storage_mod.os, "fsync")
    OsFiles(str(tmp_path / "x.lock")).create(str(tmp_path / "record"), b"data", durable=durable)
    assert fsync.call_count == durable
    assert (tmp_path / "record").read_bytes() == b"data"


def test_link_trusts_identity_over_an_error(tmp_path: Path, mocker: MockerFixture) -> None:
    # NFS can report an error after a successful link.
    real_link = storage_mod.link_no_follow

    def link_then_fail(source: str, target: str, **dir_fds: int | None) -> None:
        real_link(source, target, **dir_fds)
        raise OSError(EIO, "Input/output error")

    mocker.patch.object(
        storage_mod,
        "link_no_follow",
        new=mocker.create_autospec(storage_mod.link_no_follow, side_effect=link_then_fail),
    )
    source = tmp_path / "source"
    source.write_bytes(b"data")
    assert OsFiles(str(tmp_path / "x.lock")).link(str(source), str(tmp_path / "target"))


@pytest.mark.skipif(not CAPABILITIES["link-dir-fd"], reason="requires descriptor-relative hard links")
def test_link_stays_in_the_open_generation_directory(  # pragma: needs link-dir-fd
    tmp_path: Path, mocker: MockerFixture
) -> None:
    files = OsFiles(str(tmp_path / "data.lock"))
    root = tmp_path / "data.lock.rw"
    files.prepare(str(root))
    directory = root / "gen"
    archived = root / "previous"
    (source := directory / "stage").write_bytes(b"snapshot")
    open_fd = os.open

    def rotate_after_open(path: str, flags: int, *, dir_fd: int | None = None) -> int:
        fd = open_fd(path, flags, dir_fd=dir_fd)
        if path == "gen" and not archived.exists():
            directory.rename(archived)
            directory.mkdir()
        return fd

    mocker.patch.object(os, "open", autospec=True, side_effect=rotate_after_open)
    assert files.link(str(source), str(target := directory / "generation"))
    assert ((archived / "generation").read_bytes(), target.exists()) == (b"snapshot", False)


def test_link_reports_an_unsupported_filesystem(tmp_path: Path, mocker: MockerFixture) -> None:
    mocker.patch.object(
        storage_mod,
        "link_no_follow",
        new=mocker.create_autospec(storage_mod.link_no_follow, side_effect=NotImplementedError("no os.link")),
    )
    source = tmp_path / "source"
    source.write_bytes(b"data")
    with pytest.raises(SoftFileLockProtocolError, match="hard-link"):
        OsFiles(str(tmp_path / "x.lock")).link(str(source), str(tmp_path / "target"))


def test_link_from_a_missing_source_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        OsFiles(str(tmp_path / "x.lock")).link(str(tmp_path / "absent"), str(tmp_path / "target"))


def test_link_raises_a_fault_that_did_not_land(tmp_path: Path, mocker: MockerFixture) -> None:
    # Treating persistent I/O errors as contention would retry forever.
    mocker.patch.object(
        storage_mod,
        "link_no_follow",
        new=mocker.create_autospec(storage_mod.link_no_follow, side_effect=OSError(EIO, "Input/output error")),
    )
    source = tmp_path / "source"
    source.write_bytes(b"data")
    with pytest.raises(OSError, match="Input/output error"):
        OsFiles(str(tmp_path / "x.lock")).link(str(source), str(tmp_path / "target"))


def test_write_head_replaces_the_previous_record(tmp_path: Path, mocker: MockerFixture) -> None:
    unlink = mocker.spy(os, "unlink")
    (target := tmp_path / "HEAD").write_bytes(b"old")
    OsFiles(str(tmp_path / "x.lock")).write_head(str(target), "stage", b"new")
    assert (target.read_bytes(), list(tmp_path.iterdir())) == (b"new", [target])
    unlink.assert_not_called()


@pytest.mark.parametrize(
    "operation",
    [
        pytest.param("filelock._soft._read_write._storage.write_all", id="write"),
        pytest.param("os.replace", id="rename"),
    ],
)
def test_write_head_cleans_up_a_failed_update(tmp_path: Path, mocker: MockerFixture, operation: str) -> None:
    (target := tmp_path / "HEAD").write_bytes(b"old")
    mocker.patch(operation, autospec=True, side_effect=PermissionError("in use"))
    with pytest.raises(PermissionError, match="in use"):
        OsFiles(str(tmp_path / "x.lock")).write_head(str(target), "stage", b"new")
    assert (target.read_bytes(), list(tmp_path.iterdir())) == (b"old", [target])


def test_write_head_keeps_a_rename_that_landed_before_an_error(tmp_path: Path, mocker: MockerFixture) -> None:
    replace = os.replace

    def rename_then_fail(source: str, target: str, **dir_fds: int | None) -> None:
        replace(source, target, **dir_fds)
        raise OSError(EIO, "rename reply lost")

    mocker.patch.object(os, "replace", autospec=True, side_effect=rename_then_fail)
    (target := tmp_path / "HEAD").write_bytes(b"old")
    with pytest.raises(OSError, match="rename reply lost"):
        OsFiles(str(tmp_path / "x.lock")).write_head(str(target), "stage", b"new")
    assert (target.read_bytes(), list(tmp_path.iterdir())) == (b"new", [target])


def test_overwrite_rewrites_the_record_it_created(tmp_path: Path) -> None:
    files = OsFiles(str(tmp_path / "x.lock"))
    identity = files.create(str(record := tmp_path / "record"), b"a=1")

    assert (files.overwrite(str(record), b"a=2", identity, b"a="), record.read_bytes()) == (True, b"a=2")


@pytest.mark.parametrize("swap", [pytest.param("link", id="hard-link"), pytest.param("rename", id="rename")])
def test_overwrite_spares_a_file_swapped_in_for_the_record(tmp_path: Path, swap: str) -> None:
    files = OsFiles(str(tmp_path / "x.lock"))
    identity = files.create(str(record := tmp_path / "record"), b"old")
    (private := tmp_path / "private").write_bytes(b"private")
    record.unlink()
    getattr(os, swap)(private, record)

    assert (files.overwrite(str(record), b"new", identity, b""), record.read_bytes()) == (False, b"private")


def test_overwrite_spares_a_file_that_inherited_the_inode_number(tmp_path: Path) -> None:
    # Model inode reuse on filesystems that allocate a fresh inode instead.
    files = OsFiles(str(tmp_path / "x.lock"))
    files.create(str(record := tmp_path / "record"), b"a=1")
    record.unlink()
    (private := tmp_path / "private").write_bytes(b"private")
    private.rename(record)
    found = record.stat()
    rewritten = files.overwrite(str(record), b"a=2", (found.st_dev, found.st_ino), b"a=")

    assert (rewritten, record.read_bytes()) == (False, b"private")


def test_overwrite_spares_a_record_with_a_second_name(tmp_path: Path) -> None:
    files = OsFiles(str(tmp_path / "x.lock"))
    identity = files.create(str(record := tmp_path / "record"), b"old")
    os.link(record, tmp_path / "private")

    assert (files.overwrite(str(record), b"new", identity, b""), record.read_bytes()) == (False, b"old")


def test_overwrite_spares_a_path_that_refuses_the_open(tmp_path: Path, mocker: MockerFixture) -> None:
    # POSIX permits ENXIO when opening a FIFO for reading and writing.
    mocker.patch.object(storage_mod.os, "open", side_effect=OSError(ENXIO, "No such device or address"), autospec=True)
    assert OsFiles(str(tmp_path / "x.lock")).overwrite(str(tmp_path / "record"), b"new", (0, 0), b"") is False


@pytest.mark.skipif(not CAPABILITIES["fifo"], reason="needs os.mkfifo")
def test_overwrite_does_not_block_on_a_fifo(tmp_path: Path) -> None:  # pragma: needs fifo
    files = OsFiles(str(tmp_path / "x.lock"))
    identity = files.create(str(record := tmp_path / "record"), b"old")
    record.unlink()
    assert sys.platform != "win32"  # skipped there; narrows os.mkfifo and os.O_NONBLOCK for the type checker
    os.mkfifo(record)
    assert files.overwrite(str(record), b"new", identity, b"") is False


@pytest.mark.skipif(not CAPABILITIES["fifo"], reason="needs os.mkfifo")
def test_overwrite_does_not_block_on_a_fifo_with_a_reader(tmp_path: Path) -> None:  # pragma: needs fifo
    files = OsFiles(str(tmp_path / "x.lock"))
    identity = files.create(str(record := tmp_path / "record"), b"old")
    record.unlink()
    assert sys.platform != "win32"  # skipped there; narrows os.mkfifo and os.O_NONBLOCK for the type checker
    os.mkfifo(record)
    reader = os.open(record, os.O_RDONLY | os.O_NONBLOCK)
    try:
        assert files.overwrite(str(record), b"new", identity, b"") is False
    finally:
        os.close(reader)


def test_unlink_tolerates_a_refused_removal(tmp_path: Path, mocker: MockerFixture) -> None:
    unlink = mocker.patch.object(storage_mod.os, "unlink", side_effect=PermissionError("held open"), autospec=True)
    OsFiles(str(tmp_path / "x.lock")).unlink(str(tmp_path / "record"))
    unlink.assert_called_once()


def test_unlink_other_errors_propagate(tmp_path: Path, mocker: MockerFixture) -> None:
    mocker.patch.object(storage_mod.os, "unlink", side_effect=OSError(EIO, "Input/output error"), autospec=True)
    with pytest.raises(OSError, match="Input/output error"):
        OsFiles(str(tmp_path / "x.lock")).unlink(str(tmp_path / "record"))


@pytest.mark.skipif(sys.platform == "win32", reason="Windows retries a refused open as a sharing race")
def test_read_refused_on_posix_raises(tmp_path: Path, mocker: MockerFixture) -> None:  # pragma: win32 no cover
    # An unreadable record must not age as a missing record.
    mocker.patch.object(storage_mod.os, "open", side_effect=PermissionError("denied"), autospec=True)
    with pytest.raises(PermissionError):
        OsFiles(str(tmp_path / "x.lock")).read(str(tmp_path / "record"))


@pytest.mark.skipif(sys.platform != "win32", reason="only Windows retries a refused open as a sharing race")
def test_read_refused_on_windows_is_missing_after_the_grace(
    tmp_path: Path, mocker: MockerFixture
) -> None:  # pragma: win32 cover
    # Windows can deny reads while deletion is pending.
    mocker.patch.object(storage_mod.os, "open", side_effect=PermissionError("sharing violation"), autospec=True)
    started = time.monotonic()
    assert OsFiles(str(tmp_path / "x.lock")).read(str(tmp_path / "record")) is None
    assert time.monotonic() - started >= 0.45


def test_listdir_stops_one_name_past_the_limit(tmp_path: Path, mocker: MockerFixture) -> None:
    mocker.patch.object(storage_mod, "MAX_LISTED_NAMES", 2)
    files = OsFiles(str(tmp_path / "x.lock"))
    files.prepare(root := str(tmp_path / "x.lock.rw"))
    for number in range(5):
        Path(root, "holders", str(number)).touch()
    assert len(files.listdir(str(Path(root, "holders")))) == 3


def test_prepare_checks_the_directories_once_unless_forced(tmp_path: Path, mocker: MockerFixture) -> None:
    files = OsFiles(str(tmp_path / "x.lock"))
    ensure = mocker.spy(storage_mod, "ensure_directory_exists")
    files.prepare(root := str(tmp_path / "x.lock.rw"))
    files.prepare(root)
    assert ensure.call_count == 1
    files.prepare(root, force=True)
    assert ensure.call_count == 2


def test_prepare_creates_missing_parents(tmp_path: Path) -> None:
    root = str(tmp_path / "nested" / "deeper" / "x.lock.rw")
    OsFiles(str(tmp_path / "nested" / "deeper" / "x.lock")).prepare(root)
    assert Path(root, "gen").is_dir()


@NEEDS_FOREIGN_STICKY_TMP
@pytest.mark.parametrize(
    "level", [pytest.param(".", id="root"), pytest.param("gen", id="gen"), pytest.param("holders", id="holders")]
)
@pytest.mark.parametrize("mode", [pytest.param(0o770, id="group"), pytest.param(0o707, id="other")])
def test_prepare_refuses_writable_tree_under_sticky_parent(
    lock_path_in_shared_tmp: Path, level: str, mode: int
) -> None:  # pragma: needs foreign-sticky-tmp
    root = Path(f"{lock_path_in_shared_tmp}.rw")
    root.mkdir(mode=0o700)
    (root / "gen").mkdir(mode=0o700)
    (root / "holders").mkdir(mode=0o700)
    (root / level).chmod(mode)

    with pytest.raises(TreeReplacedError, match="writable by other accounts"):
        OsFiles(str(lock_path_in_shared_tmp)).prepare(str(root))


@NEEDS_FOREIGN_STICKY_TMP
@pytest.mark.parametrize("name_only", [pytest.param(False, id="descriptors"), pytest.param(True, id="names")])
def test_replaced_tree_cannot_be_writable_under_sticky_parent(
    lock_path_in_shared_tmp: Path, mocker: MockerFixture, *, name_only: bool
) -> None:  # pragma: needs foreign-sticky-tmp
    mocker.patch.object(storage_mod, "_SUPPORTS_DIR_FD", new=not name_only and storage_mod._SUPPORTS_DIR_FD)
    root = Path(f"{lock_path_in_shared_tmp}.rw")
    files = OsFiles(str(lock_path_in_shared_tmp))
    files.prepare(str(root))
    root.rename(aside := Path(f"{root}.aside"))
    try:
        root.mkdir(mode=0o700)
        root.chmod(0o770)
        (root / "holders").mkdir(mode=0o700)
        with pytest.raises(TreeReplacedError, match="writable by other accounts"):
            files.listdir(str(root / "holders"))
    finally:
        shutil.rmtree(aside)


@NEEDS_FOREIGN_STICKY_TMP
@pytest.mark.parametrize("level", [pytest.param(".", id="root"), pytest.param("holders", id="holders")])
@pytest.mark.parametrize("name_only", [pytest.param(False, id="descriptors"), pytest.param(True, id="names")])
def test_live_tree_refuses_writable_mode_change(
    lock_path_in_shared_tmp: Path, mocker: MockerFixture, level: str, *, name_only: bool
) -> None:  # pragma: needs foreign-sticky-tmp
    mocker.patch.object(storage_mod, "_SUPPORTS_DIR_FD", new=not name_only and storage_mod._SUPPORTS_DIR_FD)
    root = Path(f"{lock_path_in_shared_tmp}.rw")
    files = OsFiles(str(lock_path_in_shared_tmp))
    files.prepare(str(root))
    (root / level).chmod(0o770)

    with pytest.raises(TreeReplacedError, match="writable by other accounts"):
        files.listdir(str(root / "holders"))


@NEEDS_FOREIGN_STICKY_TMP
def test_live_tree_rechecks_owner_with_same_inode(
    lock_path_in_shared_tmp: Path, mocker: MockerFixture
) -> None:  # pragma: needs foreign-sticky-tmp
    root = Path(f"{lock_path_in_shared_tmp}.rw")
    files = OsFiles(str(lock_path_in_shared_tmp))
    files.prepare(str(root))
    assert sys.platform != "win32"  # the capability is absent there; narrows os.geteuid for the type checker
    mocker.patch("filelock._filesystem._files.os.geteuid", autospec=True, return_value=os.geteuid() + 1)

    with pytest.raises(TreeReplacedError, match="not to this process's account"):
        files.listdir(str(root / "holders"))


@NEEDS_FOREIGN_STICKY_TMP
@pytest.mark.parametrize("name_only", [pytest.param(False, id="descriptors"), pytest.param(True, id="names")])
@pytest.mark.parametrize(
    "call",
    [
        pytest.param(lambda files, path: files.create(path, b"data"), id="create"),
        pytest.param(lambda files, path: files.read(path), id="read"),
        pytest.param(lambda files, path: files.listdir(str(Path(path).parent)), id="listdir"),
        pytest.param(lambda files, path: files.unlink(path), id="unlink"),
    ],
)
def test_record_operations_recheck_the_owner_of_a_replaced_directory(  # pragma: needs foreign-sticky-tmp
    lock_path_in_shared_tmp: Path, mocker: MockerFixture, call: Callable[[OsFiles, str], object], name_only: bool
) -> None:
    mocker.patch.object(storage_mod, "_SUPPORTS_DIR_FD", new=not name_only and storage_mod._SUPPORTS_DIR_FD)
    files = OsFiles(str(lock_path_in_shared_tmp))
    files.prepare(root := f"{lock_path_in_shared_tmp}.rw")
    Path(root).rename(aside := f"{root}.aside")
    try:
        for name in ("gen", "holders"):
            Path(root, name).mkdir(parents=True)
        assert sys.platform != "win32"  # the capability is absent there; narrows os.geteuid for the type checker
        mocker.patch("filelock._filesystem._files.os.geteuid", autospec=True, return_value=os.geteuid() + 1)
        with pytest.raises(RuntimeError, match="not to this process's account"):
            call(files, str(Path(root, "holders", "record")))
        assert list(Path(root, "holders").iterdir()) == []
    finally:
        shutil.rmtree(aside, ignore_errors=True)


def test_record_operations_round_trip_without_dir_fd(tmp_path: Path, mocker: MockerFixture) -> None:
    mocker.patch.object(storage_mod, "_SUPPORTS_DIR_FD", new=False)
    files = OsFiles(str(tmp_path / "x.lock"))
    files.prepare(str(tmp_path / "x.lock.rw"))
    record: Final[str] = str(tmp_path / "x.lock.rw" / "holders" / "record")
    files.create(record, b"data")
    assert files.read(record) == b"data"
    assert files.listdir(str(Path(record).parent)) == ["record"]
    files.unlink(record)
    assert files.read(record) is None
