from __future__ import annotations

import errno
import os
from errno import EIO
from pathlib import Path
from typing import TYPE_CHECKING, Final

import pytest
from capabilities import CAPABILITIES

from filelock._soft._read_write._storage import OsFiles, TreeReplacedError

from .conftest import make_participant

if TYPE_CHECKING:
    from collections.abc import Callable

    from pytest_mock import MockerFixture


pytestmark: Final[pytest.MarkDecorator] = pytest.mark.usefixtures("flushes")


@pytest.mark.requires_hard_links
def test_heartbeat_ignores_a_broken_successor_record(lock_file: str, files: OsFiles, root: str) -> None:
    # A corrupt successor does not establish loss of the current hold.
    writer = make_participant(files, lock_file, "write")
    writer.publish()
    assert writer.advance()
    Path(root, "gen", f"{2:020d}").write_bytes(b"garbage\n")
    assert writer.heartbeat() == ("ok", None)


@pytest.mark.requires_hard_links
def test_heartbeat_reports_the_refresh_filesystem_error(lock_file: str, files: OsFiles, mocker: MockerFixture) -> None:
    writer = make_participant(files, lock_file, "write")
    writer.publish()
    assert writer.advance()
    error = OSError(EIO, "Input/output error")
    mocker.patch.object(OsFiles, "overwrite", autospec=True, side_effect=error)
    assert writer.heartbeat() == ("transient", error)


@pytest.mark.requires_hard_links
def test_heartbeat_reports_a_replaced_tree_as_lost(lock_file: str, files: OsFiles, mocker: MockerFixture) -> None:
    writer = make_participant(files, lock_file, "write")
    writer.publish()
    assert writer.advance()
    mocker.patch.object(OsFiles, "overwrite", autospec=True, side_effect=TreeReplacedError("tree replaced"))
    assert writer.heartbeat() == ("lost", None)


def test_heartbeat_without_a_published_record_is_lost(lock_file: str, files: OsFiles) -> None:
    assert make_participant(files, lock_file, "write").heartbeat() == ("lost", None)


@pytest.mark.skipif(
    not (CAPABILITIES["symlink"] and CAPABILITIES["link-dir-fd"]),
    reason="a swapped tree is refused only where records are reached through descriptors",
)
@pytest.mark.requires_hard_links
def test_heartbeat_with_the_tree_swapped_for_a_symlink_is_lost(  # pragma: needs link-dir-fd
    lock_file: str, files: OsFiles, root: str, tmp_path: Path
) -> None:
    writer = make_participant(files, lock_file, "write")
    writer.publish()
    assert writer.advance()
    Path(root).rename(tmp_path / "aside")
    Path(root).symlink_to(target := tmp_path / "target", target_is_directory=True)
    target.mkdir()

    assert (writer.heartbeat(), list(target.iterdir())) == (("lost", None), [])


@pytest.mark.skipif(
    not (CAPABILITIES["symlink"] and CAPABILITIES["link-dir-fd"]),
    reason="a swapped tree is refused only where records are reached through descriptors",
)
@pytest.mark.requires_hard_links
@pytest.mark.parametrize(
    "component",
    [pytest.param(Path, id="root"), pytest.param(lambda root: Path(root, "holders"), id="holders")],
)
@pytest.mark.parametrize("refusal", ["EFTYPE", "EMLINK"])
def test_heartbeat_with_a_symlink_refused_by_platform_errno_is_lost(  # pragma: needs link-dir-fd
    lock_file: str,
    files: OsFiles,
    root: str,
    tmp_path: Path,
    mocker: MockerFixture,
    component: Callable[[str], Path],
    refusal: str,
) -> None:
    writer = make_participant(files, lock_file, "write")
    writer.publish()
    assert writer.advance()
    directory = component(root)
    directory.rename(tmp_path / "aside")
    directory.symlink_to(target := tmp_path / "target", target_is_directory=True)
    target.mkdir()
    eftype = getattr(errno, "EFTYPE", 79)
    mocker.patch.object(errno, "EFTYPE", eftype, create=True)
    refusal_errno = getattr(errno, refusal)
    real_open = os.open
    refusals: list[tuple[int, int | None]] = []

    def platform_open(path: str, flags: int, mode: int = 0o777, *, dir_fd: int | None = None) -> int:
        try:
            return real_open(path, flags, mode, dir_fd=dir_fd)
        except OSError as error:
            # FreeBSD and NetBSD report different errors for the same symlink.
            refusals.append((flags, error.errno))
            raise OSError(refusal_errno, os.strerror(refusal_errno), path) from error

    mocker.patch.object(os, "open", side_effect=platform_open, autospec=True)

    assert (writer.heartbeat(), list(target.iterdir())) == (("lost", None), [])
    assert len(refusals) == 1
    flags, native_errno = refusals[0]
    assert flags & getattr(os, "O_NOFOLLOW", 0)
    assert native_errno in {errno.ELOOP, errno.ENOTDIR, errno.EMLINK, eftype}


@pytest.mark.requires_hard_links
def test_heartbeat_leaves_a_file_linked_over_the_holder_record_alone(lock_file: str, files: OsFiles, root: str) -> None:
    writer = make_participant(files, lock_file, "write")
    writer.publish()
    (private := Path(f"{lock_file}.private")).write_bytes(b"private")
    os.link(private, linked := Path(root, "holders", "linked"))
    linked.replace(Path(root, "holders", writer.token))
    assert writer.advance()

    assert (writer.heartbeat(), private.read_bytes()) == (("lost", None), b"private")


@pytest.mark.requires_hard_links
def test_heartbeat_leaves_a_file_with_the_holder_record_inode_number_alone(
    lock_file: str, files: OsFiles, root: str
) -> None:
    # Matching inode numbers do not prove the replacement contains the original holder record.
    writer = make_participant(files, lock_file, "write")
    writer.publish()
    assert writer.advance()
    (record := Path(root, "holders", writer.token)).write_bytes(b"private")

    assert (writer.heartbeat(), record.read_bytes()) == (("lost", None), b"private")


@pytest.mark.requires_hard_links
def test_heartbeat_keeps_an_intact_record_after_short_reads(
    lock_file: str, files: OsFiles, root: str, mocker: MockerFixture
) -> None:
    writer = make_participant(files, lock_file, "write")
    writer.publish()
    assert writer.advance()
    before = (record := Path(root, "holders", writer.token)).read_bytes()
    real_read = os.read
    mocker.patch.object(os, "read", autospec=True, side_effect=lambda fd, size: real_read(fd, min(size, 3)))

    assert writer.heartbeat() == ("ok", None)
    assert record.read_bytes() != before
