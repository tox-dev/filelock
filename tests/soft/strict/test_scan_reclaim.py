from __future__ import annotations

import os
import sys
import time
from errno import EACCES, EIO, ENOSPC
from pathlib import Path
from typing import TYPE_CHECKING, Final

import pytest
from capabilities import CAPABILITIES

from filelock import SoftFileLockProtocolError, StrictSoftFileLock, Timeout
from filelock._soft._strict._claims import _PRIVATE_RECORD_MARKER

if TYPE_CHECKING:
    from collections.abc import Callable

    from pytest_mock import MockerFixture
from .claim_records import initialize_protocol, private_claim_path, write_foreign_claim
from .filesystem_faults import PathValue, patch_strict_clock

pytestmark: Final[pytest.MarkDecorator] = pytest.mark.requires_hard_links


_POSIX_PRIVATE_RECLAIM: Final[pytest.MarkDecorator] = pytest.mark.skipif(
    sys.platform == "win32", reason="Windows does not reclaim private records during scans"
)


def _stage_claim_record(lock_path: Path) -> Path:
    initialize_protocol(lock_path)
    return private_claim_path(lock_path, "0" * 32)


def _stage_sentinel_record(lock_path: Path) -> Path:
    return lock_path.parent / f".{lock_path.name}{_PRIVATE_RECORD_MARKER}{'0' * 32}.tmp"


_NON_REGULAR_NODES: Final[list[pytest.ParameterSet]] = [
    pytest.param(Path.mkdir, Path.is_dir, id="directory"),
    pytest.param(
        lambda path: path.symlink_to(path.parent / "missing"),
        Path.is_symlink,
        id="symlink",
        marks=pytest.mark.skipif(not CAPABILITIES["symlink"], reason="stages the private record as a symlink"),
    ),
]


@pytest.mark.parametrize(("make_node", "is_node"), _NON_REGULAR_NODES)
def test_strict_soft_reaper_leaves_non_regular_private_node(
    tmp_path: Path, make_node: Callable[[Path], object], is_node: Callable[[Path], bool]
) -> None:
    lock_path = tmp_path / "resource.lock"
    initialize_protocol(lock_path)
    private_path = private_claim_path(lock_path, "0" * 32)
    make_node(private_path)  # pragma: needs symlink

    with pytest.raises(SoftFileLockProtocolError, match="cannot list claim directory"):
        _ = StrictSoftFileLock(lock_path).claims
    assert is_node(private_path)


@pytest.mark.parametrize(("make_node", "is_node"), _NON_REGULAR_NODES)
def test_strict_soft_non_regular_sentinel_private_node_fails_closed(
    tmp_path: Path, make_node: Callable[[Path], object], is_node: Callable[[Path], bool]
) -> None:
    lock_path = tmp_path / "resource.lock"
    private_path = tmp_path / f".{lock_path.name}{_PRIVATE_RECORD_MARKER}{'0' * 32}.tmp"
    make_node(private_path)  # pragma: needs symlink

    with pytest.raises(SoftFileLockProtocolError, match="cannot reclaim sentinel private records"):
        StrictSoftFileLock(lock_path, timeout=0).acquire()
    assert is_node(private_path)


@_POSIX_PRIVATE_RECLAIM  # pragma: win32 no cover
def test_strict_soft_reaper_does_not_retry_sharing_error(
    abandoned_record: tuple[Path, Path], mocker: MockerFixture
) -> None:
    lock_path, private_path = abandoned_record
    real_unlink = os.unlink

    def deny_abandoned_record(path: PathValue, *, dir_fd: int | None = None) -> None:
        if Path(os.fsdecode(path)).name == private_path.name:
            raise PermissionError(EACCES, "sharing violation")
        real_unlink(path, dir_fd=dir_fd)

    unlink = mocker.patch("os.unlink", autospec=True, side_effect=deny_abandoned_record)
    sleep = mocker.create_autospec(time.sleep)
    patch_strict_clock(mocker, "_scan", sleep=sleep)
    # A contender blocked by a foreign claim ages the record out in its scan without removing a claim of its own.
    write_foreign_claim(Path(f"{lock_path}.filelock") / "claims", "f" * 32)

    with pytest.raises(Timeout):
        StrictSoftFileLock(lock_path, timeout=0).acquire()
    reaper_unlinks = [Path(os.fsdecode(call.args[0])).name for call in unlink.call_args_list].count(private_path.name)
    assert (reaper_unlinks, sleep.call_count, private_path.exists()) == (1, 0, True)


@pytest.mark.parametrize(
    "stage_record",
    [pytest.param(_stage_claim_record, id="claim"), pytest.param(_stage_sentinel_record, id="sentinel")],
)
def test_strict_soft_reaper_ages_records_by_filesystem_clock(
    tmp_path: Path, mocker: MockerFixture, stage_record: Callable[[Path], Path]
) -> None:
    lock_path = tmp_path / "resource.lock"
    private_path = stage_record(lock_path)
    private_path.write_bytes(b"in-flight publication")
    # A client clock an hour ahead of the filesystem's would read the fresh record as abandoned.
    patch_strict_clock(mocker, "_scan", wall=lambda: time.time() + 3600)

    with StrictSoftFileLock(lock_path, timeout=0):
        assert private_path.read_bytes() == b"in-flight publication"


@_POSIX_PRIVATE_RECLAIM  # pragma: win32 no cover
def test_strict_soft_reaper_without_filesystem_clock_keeps_records(
    abandoned_record: tuple[Path, Path], mocker: MockerFixture
) -> None:
    lock_path, private_path = abandoned_record
    real_open = os.open

    def deny_clock_probe(path: PathValue, flags: int, mode: int = 0o777, *, dir_fd: int | None = None) -> int:
        if Path(os.fsdecode(path)).name.startswith(".held-"):
            raise PermissionError(EACCES, "read-only observer")
        return real_open(path, flags, mode, dir_fd=dir_fd)

    mocker.patch("os.open", autospec=True, side_effect=deny_clock_probe)
    # A contender blocked by a foreign claim scans with the clock probe without publishing a claim of its own.
    write_foreign_claim(Path(f"{lock_path}.filelock") / "claims", "f" * 32)

    with pytest.raises(Timeout):
        StrictSoftFileLock(lock_path, timeout=0).acquire()
    assert private_path.exists()


def test_strict_soft_claims_inspection_creates_no_file(
    abandoned_record: tuple[Path, Path], mocker: MockerFixture
) -> None:
    lock_path, private_path = abandoned_record
    opened = mocker.spy(os, "open")

    assert StrictSoftFileLock(lock_path).claims == ()
    assert ([call.args for call in opened.call_args_list if call.args[1] & os.O_CREAT], private_path.exists()) == (
        [],
        True,
    )


def _fail_probe_stamp(mocker: MockerFixture) -> None:  # pragma: win32 no cover
    real_write = os.write

    def enospc_on_probe(fd: int, data: bytes) -> int:
        if data == b"\n":
            raise OSError(ENOSPC, "No space left on device")
        return real_write(fd, data)

    mocker.patch("os.write", autospec=True, side_effect=enospc_on_probe)


def _fail_probe_removal(mocker: MockerFixture) -> None:  # pragma: win32 no cover
    real_unlink = os.unlink

    def eio_on_probe(path: PathValue, *, dir_fd: int | None = None) -> None:  # pragma: win32 no cover
        if Path(os.fsdecode(path)).name.startswith(".held-v1-"):
            raise OSError(EIO, "probe unlink failed")
        real_unlink(path, dir_fd=dir_fd)

    mocker.patch("os.unlink", autospec=True, side_effect=eio_on_probe)


@_POSIX_PRIVATE_RECLAIM  # pragma: win32 no cover
@pytest.mark.parametrize(
    ("fail_probe", "kept"),
    [pytest.param(_fail_probe_stamp, True, id="stamp"), pytest.param(_fail_probe_removal, False, id="remove")],
)
def test_strict_soft_failed_clock_probe_does_not_fail_the_scan(
    abandoned_record: tuple[Path, Path],
    mocker: MockerFixture,
    fail_probe: Callable[[MockerFixture], None],
    *,
    kept: bool,
) -> None:
    lock_path, private_path = abandoned_record
    fail_probe(mocker)

    with StrictSoftFileLock(lock_path, timeout=0):
        assert private_path.exists() is kept
