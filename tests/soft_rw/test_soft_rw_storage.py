"""The :class:`~filelock._soft_rw._storage.OsFiles` implementation of the protocol's filesystem operations."""

from __future__ import annotations

import os
import sys
import time
from errno import EIO
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from filelock import SoftFileLockProtocolError
from filelock._soft_rw import _storage as storage_mod
from filelock._soft_rw._storage import OsFiles

if TYPE_CHECKING:
    from pytest_mock import MockerFixture

pytestmark = pytest.mark.requires_hard_links


def test_read_missing_file_is_none(tmp_path: Path) -> None:
    assert OsFiles(str(tmp_path / "x.lock")).read(str(tmp_path / "absent")) is None


def test_read_other_errors_propagate(tmp_path: Path, mocker: MockerFixture) -> None:
    mocker.patch.object(storage_mod.os, "open", side_effect=OSError(EIO, "Input/output error"))
    with pytest.raises(OSError, match="Input/output error"):
        OsFiles(str(tmp_path / "x.lock")).read(str(tmp_path / "absent"))


def test_read_non_regular_file_is_empty(tmp_path: Path) -> None:
    # The null device is a character device on every platform, so it stands in for anything planted at a record path
    # that is not a regular file.
    assert OsFiles(str(tmp_path / "x.lock")).read(os.devnull) == b""


def test_create_rolls_back_a_failed_write(tmp_path: Path, mocker: MockerFixture) -> None:
    mocker.patch.object(storage_mod, "write_all", side_effect=OSError("write boom"))
    with pytest.raises(OSError, match="write boom"):
        OsFiles(str(tmp_path / "x.lock")).create(str(tmp_path / "record"), b"data")
    assert not (tmp_path / "record").exists()


def test_link_trusts_identity_over_an_error(tmp_path: Path, mocker: MockerFixture) -> None:
    # An NFS retransmit can report a link that landed as failed; the target naming the source's file is what counts.
    real_link = storage_mod._link_no_follow

    def link_then_fail(source: str, target: str) -> None:
        real_link(source, target)
        raise OSError(EIO, "Input/output error")

    mocker.patch.object(storage_mod, "_link_no_follow", side_effect=link_then_fail)
    source = tmp_path / "source"
    source.write_bytes(b"data")
    assert OsFiles(str(tmp_path / "x.lock")).link(str(source), str(tmp_path / "target"))


def test_link_reports_an_unsupported_filesystem(tmp_path: Path, mocker: MockerFixture) -> None:
    mocker.patch.object(storage_mod, "_link_no_follow", side_effect=NotImplementedError("no os.link"))
    source = tmp_path / "source"
    source.write_bytes(b"data")
    with pytest.raises(SoftFileLockProtocolError, match="hard-link"):
        OsFiles(str(tmp_path / "x.lock")).link(str(source), str(tmp_path / "target"))


def test_link_from_a_missing_source_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        OsFiles(str(tmp_path / "x.lock")).link(str(tmp_path / "absent"), str(tmp_path / "target"))


def test_link_raises_a_fault_that_did_not_land(tmp_path: Path, mocker: MockerFixture) -> None:
    # A persistent EPERM or EIO is not a peer winning the race; reporting it as one would spin forever.
    mocker.patch.object(storage_mod, "_link_no_follow", side_effect=OSError(EIO, "Input/output error"))
    source = tmp_path / "source"
    source.write_bytes(b"data")
    with pytest.raises(OSError, match="Input/output error"):
        OsFiles(str(tmp_path / "x.lock")).link(str(source), str(tmp_path / "target"))


def test_overwrite_missing_file_is_false(tmp_path: Path) -> None:
    assert not OsFiles(str(tmp_path / "x.lock")).overwrite(str(tmp_path / "absent"), b"data")


def test_overwrite_other_errors_propagate(tmp_path: Path, mocker: MockerFixture) -> None:
    mocker.patch.object(storage_mod.os, "open", side_effect=OSError(EIO, "Input/output error"))
    with pytest.raises(OSError, match="Input/output error"):
        OsFiles(str(tmp_path / "x.lock")).overwrite(str(tmp_path / "absent"), b"data")


def test_unlink_tolerates_a_refused_removal(tmp_path: Path, mocker: MockerFixture) -> None:
    mocker.patch.object(Path, "unlink", side_effect=PermissionError("held open"))
    OsFiles(str(tmp_path / "x.lock")).unlink(str(tmp_path / "record"))


def test_unlink_other_errors_propagate(tmp_path: Path, mocker: MockerFixture) -> None:
    mocker.patch.object(Path, "unlink", side_effect=OSError(EIO, "Input/output error"))
    with pytest.raises(OSError, match="Input/output error"):
        OsFiles(str(tmp_path / "x.lock")).unlink(str(tmp_path / "record"))


def test_listdir_missing_directory_is_empty(tmp_path: Path) -> None:
    assert OsFiles(str(tmp_path / "x.lock")).listdir(str(tmp_path / "absent")) == []


@pytest.mark.skipif(sys.platform == "win32", reason="Windows retries a refused open as a sharing race")
def test_read_refused_on_posix_raises(tmp_path: Path, mocker: MockerFixture) -> None:  # pragma: win32 no cover
    # Reading a peer's record as missing when it is unreadable would age it as a constant and evict a holder.
    mocker.patch.object(storage_mod.os, "open", side_effect=PermissionError("denied"))
    with pytest.raises(PermissionError):
        OsFiles(str(tmp_path / "x.lock")).read(str(tmp_path / "record"))


@pytest.mark.skipif(sys.platform != "win32", reason="only Windows retries a refused open as a sharing race")
def test_read_refused_on_windows_is_missing_after_the_grace(
    tmp_path: Path, mocker: MockerFixture
) -> None:  # pragma: win32 cover
    # A file still refusing after the grace has been delete-pending longer than any deletion takes: on its way out.
    mocker.patch.object(storage_mod.os, "open", side_effect=PermissionError("sharing violation"))
    started = time.monotonic()
    assert OsFiles(str(tmp_path / "x.lock")).read(str(tmp_path / "record")) is None
    assert time.monotonic() - started >= storage_mod._WINDOWS_OPEN_GRACE - 0.05


def test_prepare_creates_missing_parents(tmp_path: Path) -> None:
    root = str(tmp_path / "nested" / "deeper" / "x.lock.rw")
    OsFiles(str(tmp_path / "nested" / "deeper" / "x.lock")).prepare(root)
    assert Path(root, "gen").is_dir()
