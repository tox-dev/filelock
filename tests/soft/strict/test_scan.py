from __future__ import annotations

import functools
import os
import stat
import sys
import time
from errno import EACCES, EIO, ESTALE
from pathlib import Path
from typing import TYPE_CHECKING, Final

import pytest

from filelock import SoftFileLockProtocolError, StrictSoftFileLock, Timeout
from filelock._soft._strict._scan import scandir_relative

if TYPE_CHECKING:
    from collections.abc import Iterator
    from contextlib import AbstractContextManager

    from pytest_mock import MockerFixture
from .capability_marks import WINDOWS_ONLY
from .claim_records import private_claim_path, write_foreign_claim, write_held_claim
from .filesystem_faults import (
    Inspect,
    PathValue,
    open_raising_for,
    patch_strict_clock,
    route_record_opens_through_os_open,
)

pytestmark: Final[pytest.MarkDecorator] = pytest.mark.requires_hard_links


@pytest.mark.parametrize(
    "published", [pytest.param(0, id="preflight"), pytest.param(1, id="intent"), pytest.param(2, id="held")]
)
def test_stale_claim_directory_withdraws_before_retry(
    initialized_lock_path: Path, mocker: MockerFixture, published: int
) -> None:
    def stale_scan(directory_ref: tuple[str, int | None]) -> AbstractContextManager[Iterator[os.DirEntry[str]]]:
        if len(list(Path(directory_ref[0]).glob("*.claim"))) == published:
            raise OSError(ESTALE, "Stale file handle")
        return scandir_relative(directory_ref)

    scan = mocker.patch("filelock._soft._strict._scan.scandir_relative", autospec=True, side_effect=stale_scan)
    lock = StrictSoftFileLock(initialized_lock_path)
    with pytest.raises(Timeout):
        lock.acquire(timeout=0)
    mocker.stop(scan)
    assert lock.claims == ()
    with lock.acquire(timeout=0):
        assert lock.is_locked


def test_claim_inspection_reports_a_stale_directory(initialized_lock_path: Path, mocker: MockerFixture) -> None:
    mocker.patch(
        "filelock._soft._strict._scan.scandir_relative", autospec=True, side_effect=OSError(ESTALE, "Stale file handle")
    )
    with pytest.raises(SoftFileLockProtocolError, match="cannot list claim directory"):
        _ = StrictSoftFileLock(initialized_lock_path).claims


def test_strict_soft_denied_claim_removed_during_grace_is_skipped(tmp_path: Path, mocker: MockerFixture) -> None:
    lock_path: Final[Path] = tmp_path / "resource.lock"
    claim: Final[Path] = write_held_claim(Path(f"{lock_path}.filelock") / "claims")
    clock: Final[Iterator[int]] = iter([0, 1])

    def tick() -> int:
        # Remove the claim as the grace runs out, after the last denied open, so only the listing recheck sees it go.
        if now := next(clock):
            claim.unlink()
        return now

    patch_strict_clock(
        mocker,
        "_scan",
        monotonic=mocker.create_autospec(time.monotonic, side_effect=tick),
        sleep=mocker.create_autospec(time.sleep),
    )
    open_raising_for(claim, PermissionError(EACCES, "sharing violation"), mocker)

    assert StrictSoftFileLock(lock_path).claims == ()


def test_strict_soft_denied_claim_still_listed_after_grace_fails_closed(tmp_path: Path, mocker: MockerFixture) -> None:
    lock_path: Final[Path] = tmp_path / "resource.lock"
    claim: Final[Path] = write_held_claim(Path(f"{lock_path}.filelock") / "claims")
    clock: Final[Iterator[int]] = iter([0, 1])
    patch_strict_clock(
        mocker,
        "_scan",
        monotonic=mocker.create_autospec(time.monotonic, side_effect=lambda: next(clock)),
        sleep=mocker.create_autospec(time.sleep),
    )
    open_raising_for(claim, PermissionError(EACCES, "sharing violation"), mocker)

    with pytest.raises(SoftFileLockProtocolError, match="cannot read claim"):
        _ = StrictSoftFileLock(lock_path).claims


@pytest.mark.skipif(sys.platform == "win32", reason="Windows has no directory mode to settle")
def test_strict_soft_directory_mode_failure_fails_closed(  # pragma: win32 no cover
    tmp_path: Path, mocker: MockerFixture
) -> None:
    mocker.patch("os.fchmod", autospec=True, side_effect=PermissionError(EACCES, "read-only mount"))

    with pytest.raises(SoftFileLockProtocolError, match="cannot set the mode"):
        StrictSoftFileLock(tmp_path / "resource.lock").acquire()


def test_strict_soft_claims_removed_one_by_one_share_one_read_grace(tmp_path: Path, mocker: MockerFixture) -> None:
    lock_path: Final[Path] = tmp_path / "resource.lock"
    (claims := Path(f"{lock_path}.filelock") / "claims").mkdir(parents=True)
    denied: Final[list[Path]] = [write_foreign_claim(claims, f"{index:032x}") for index in range(10)]
    now = 0.0

    def tick(seconds: float) -> None:
        # Another process removes the lowest unreadable claim every 0.2 s, inside the grace a single claim gets.
        nonlocal now
        before, now = now, now + seconds
        if int(now / 0.2) > int(before / 0.2) and (remaining := [claim for claim in denied if claim.exists()]):
            remaining[0].unlink()

    real_open = os.open

    def deny_claims(path: PathValue, flags: int, mode: int = 0o777, *, dir_fd: int | None = None) -> int:
        if os.fsdecode(path).endswith(".claim"):
            raise PermissionError(EACCES, "claim read denied")
        return real_open(path, flags, mode, dir_fd=dir_fd)  # pragma: needs dir-fd

    patch_strict_clock(
        mocker,
        "_scan",
        monotonic=mocker.create_autospec(time.monotonic, side_effect=lambda: now),
        sleep=mocker.create_autospec(time.sleep, side_effect=tick),
    )
    route_record_opens_through_os_open(mocker)
    mocker.patch("os.open", autospec=True, side_effect=deny_claims)

    with pytest.raises(SoftFileLockProtocolError, match="cannot read claim"):
        _ = StrictSoftFileLock(lock_path).claims
    assert now < 1


@WINDOWS_ONLY  # pragma: win32 cover
def test_strict_soft_claim_pending_deletion_is_skipped(tmp_path: Path) -> None:
    # Windows can deny opens of a withdrawn claim until the peer closes its last handle.
    assert sys.platform == "win32"
    import ctypes

    lock_path = tmp_path / "resource.lock"
    claim = write_held_claim(Path(f"{lock_path}.filelock") / "claims")
    kernel32 = ctypes.WinDLL("kernel32")  # a private instance, so the restype below stays local
    kernel32.CreateFileW.restype = ctypes.c_void_p
    delete_access, share_all, open_existing, file_disposition_info = 0x10000, 0x1 | 0x2 | 0x4, 3, 4
    handle = ctypes.c_void_p(kernel32.CreateFileW(str(claim), delete_access, share_all, None, open_existing, 0, None))
    delete_file = ctypes.c_bool(True)  # the FILE_DISPOSITION_INFO BOOLEAN
    try:
        assert kernel32.SetFileInformationByHandle(
            handle, file_disposition_info, ctypes.byref(delete_file), ctypes.sizeof(delete_file)
        )
        assert StrictSoftFileLock(lock_path).claims == ()
    finally:
        kernel32.CloseHandle(handle)


def test_strict_soft_claim_removed_between_inspection_and_open_is_skipped(
    tmp_path: Path, mocker: MockerFixture
) -> None:
    claim: Final[Path] = write_held_claim(Path(f"{tmp_path / 'resource.lock'}.filelock") / "claims")

    def remove_after_inspection(inspect: Inspect) -> Inspect:
        @functools.wraps(inspect)
        def inspect_then_remove(path: PathValue, **options: int | bool | None) -> os.stat_result:
            found = inspect(path, **options)
            # Windows inspects nothing else in this scan; with dir_fd the claim directory is stat-ed too.
            if Path(os.fsdecode(path)).name == claim.name:  # pragma: no branch
                claim.unlink()
            return found

        return inspect_then_remove

    # The lock inspects the claim with lstat by path, or with stat relative to its directory where dir_fd works.
    for function in ("lstat", "stat"):
        mocker.patch(
            f"os.{function}",
            side_effect=remove_after_inspection(getattr(os, function)),
            autospec=True,
        )

    assert StrictSoftFileLock(tmp_path / "resource.lock").claims == ()


def test_strict_soft_owner_releases_while_a_contender_reads_its_claim(
    initialized_lock_path: Path, mocker: MockerFixture
) -> None:
    # On Windows a reader that opened the claim without FILE_SHARE_DELETE made the owner's unlink fail until it closed.
    (holder := StrictSoftFileLock(initialized_lock_path)).acquire()
    real_read = os.read

    def release_mid_read(fd: int, length: int) -> bytes:
        # The scan reads this one claim and nothing else, so the holder releases once.
        holder.release()
        return real_read(fd, length)

    mocker.patch("os.read", autospec=True, side_effect=release_mid_read)
    _ = StrictSoftFileLock(initialized_lock_path).claims

    assert (holder.is_locked, StrictSoftFileLock(initialized_lock_path).claims) == (False, ())


def test_strict_soft_claims_recheck_after_a_denied_claim_creates_no_file(tmp_path: Path, mocker: MockerFixture) -> None:
    lock_path: Final[Path] = tmp_path / "resource.lock"
    claim: Final[Path] = write_held_claim(Path(f"{lock_path}.filelock") / "claims")
    (private_path := private_claim_path(lock_path, "0" * 32)).write_bytes(b"abandoned")
    os.utime(private_path, (0, 0))
    patch_strict_clock(
        mocker,
        "_scan",
        monotonic=mocker.create_autospec(time.monotonic, side_effect=[0, 1]),
        sleep=mocker.create_autospec(time.sleep),
    )
    opened = open_raising_for(claim, PermissionError(EACCES, "sharing violation"), mocker)

    with pytest.raises(SoftFileLockProtocolError, match="cannot read claim"):
        _ = StrictSoftFileLock(lock_path).claims
    assert ([call.args for call in opened.call_args_list if call.args[1] & os.O_CREAT], private_path.exists()) == (
        [],
        True,
    )


def test_strict_soft_claim_read_retries_after_a_slow_denied_attempt(tmp_path: Path, mocker: MockerFixture) -> None:
    # A slow first attempt must still get the retry granted by the read grace.
    lock_path = tmp_path / "resource.lock"
    claim = write_held_claim(Path(f"{lock_path}.filelock") / "claims")
    real_open = os.open
    denied = False
    patch_strict_clock(mocker, "_scan", monotonic=lambda: float(denied))

    def deny_slowly_once(path: PathValue, flags: int, mode: int = 0o777, *, dir_fd: int | None = None) -> int:
        nonlocal denied
        if not denied and Path(os.fsdecode(path)).name == claim.name:
            denied = True
            raise PermissionError(EACCES, "sharing violation")
        return real_open(path, flags, mode, dir_fd=dir_fd)

    route_record_opens_through_os_open(mocker)
    mocker.patch("os.open", side_effect=deny_slowly_once, autospec=True)

    assert ([found.name for found in StrictSoftFileLock(lock_path).claims], denied) == ([claim.name], True)


def test_strict_soft_ignores_malformed_private_record_name(tmp_path: Path) -> None:
    lock_path = tmp_path / "resource.lock"
    claims = Path(f"{lock_path}.filelock") / "claims"
    claims.mkdir(parents=True)
    (claims / ".decoy.tmp").write_bytes(b"junk")

    assert StrictSoftFileLock(lock_path).claims == ()


def test_strict_soft_record_read_and_close_failure_group(tmp_path: Path, mocker: MockerFixture) -> None:
    lock_path = tmp_path / "resource.lock"
    claims = Path(f"{lock_path}.filelock") / "claims"
    claims.mkdir(parents=True)
    (claims / f"held-v1-{'0' * 32}.claim").write_bytes(b"x" * 1025)
    real_close = os.close
    real_fstat = os.fstat

    def fail_close(fd: int) -> None:
        # Reading the single oversized claim closes one record, so failing every such close still fails once.
        is_directory = stat.S_ISDIR(real_fstat(fd).st_mode)
        real_close(fd)
        if not is_directory:  # pragma: needs dir-fd
            raise OSError(EIO, "record close failed")

    mocker.patch("os.close", side_effect=fail_close, autospec=True)

    with pytest.raises(BaseExceptionGroup) as raised:
        _ = StrictSoftFileLock(lock_path).claims
    messages = [str(error) for error in raised.value.exceptions]
    assert (
        any("exceeds 1024 bytes" in message for message in messages),
        "[Errno 5] record close failed" in messages,
    ) == (
        True,
        True,
    )
