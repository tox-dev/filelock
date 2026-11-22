from __future__ import annotations

import os
import secrets
from errno import EEXIST
from pathlib import Path
from typing import TYPE_CHECKING, Final

import pytest

from filelock import StrictSoftFileLock, Timeout
from filelock._soft._strict._scan import _list_claim_names
from tests.capability_marks import (
    NEEDS_UNLINK_OPEN_FILE,
)

if TYPE_CHECKING:
    from pytest_mock import MockerFixture

    from .filesystem_faults import PathValue
from .capability_marks import CLAIM_LINK_ONLY, NEEDS_DIR_FD
from .claim_records import initialize_protocol, private_claim_path

pytestmark: Final[pytest.MarkDecorator] = pytest.mark.requires_hard_links


@pytest.mark.parametrize("state", ["intent", "held"])
@CLAIM_LINK_ONLY  # pragma: win32 no cover
def test_strict_soft_lost_link_reply_counts_as_committed(
    initialized_lock_path: Path, mocker: MockerFixture, state: str
) -> None:
    real_link = os.link

    def link_then_report_exists(
        source: PathValue,
        destination: PathValue,
        *,
        src_dir_fd: int | None = None,
        dst_dir_fd: int | None = None,
        follow_symlinks: bool = True,
    ) -> None:
        # An NFS retransmit of a LINK whose reply was lost: the first request committed, the retry sees the name.
        real_link(source, destination, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd, follow_symlinks=follow_symlinks)
        if Path(os.fsdecode(destination)).name.startswith(f"{state}-"):
            raise FileExistsError(EEXIST, "retransmitted link")

    mocker.patch("os.link", autospec=True, side_effect=link_then_report_exists)

    with (lock := StrictSoftFileLock(initialized_lock_path, timeout=0)):
        assert (lock.is_locked, sorted(claim.state for claim in lock.claims)) == (True, ["held", "intent"])
    assert lock.claims == ()


@NEEDS_UNLINK_OPEN_FILE  # pragma: needs unlink-open-file
@CLAIM_LINK_ONLY  # pragma: win32 no cover
def test_strict_soft_lost_link_reply_survives_a_peer_reaping_the_source(
    initialized_lock_path: Path, mocker: MockerFixture
) -> None:
    real_link = os.link

    def link_then_peer_reaps(
        source: PathValue,
        destination: PathValue,
        *,
        src_dir_fd: int | None = None,
        dst_dir_fd: int | None = None,
        follow_symlinks: bool = True,
    ) -> None:
        real_link(source, destination, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd, follow_symlinks=follow_symlinks)
        if Path(os.fsdecode(destination)).name.startswith("intent-"):
            # A contender's scan reaps the private record as soon as its link count reaches two, before the
            # retransmitted LINK answers EEXIST.
            Path(f"{initialized_lock_path}.filelock", "claims", os.fsdecode(source)).unlink()
            raise FileExistsError(EEXIST, "retransmitted link")

    mocker.patch("os.link", autospec=True, side_effect=link_then_peer_reaps)

    with (lock := StrictSoftFileLock(initialized_lock_path, timeout=0)):
        assert (lock.is_locked, sorted(claim.state for claim in lock.claims)) == (True, ["held", "intent"])
    assert lock.claims == ()


@pytest.mark.parametrize("scan_to_empty", [pytest.param(2, id="intent"), pytest.param(3, id="held")])
def test_strict_soft_force_break_during_doorway_backs_off(
    tmp_path: Path, mocker: MockerFixture, scan_to_empty: int
) -> None:
    lock_path = tmp_path / "resource.lock"
    initialize_protocol(lock_path)
    claims = Path(f"{lock_path}.filelock", "claims")
    real_list = _list_claim_names
    scans = 0

    def break_before_scan(
        lock_file: str, directory_ref: tuple[str, int | None], *, entry_limit: int | None
    ) -> list[str]:
        nonlocal scans
        scans += 1
        if scans == scan_to_empty:
            for claim in claims.glob("*.claim"):
                claim.unlink()
        return real_list(lock_file, directory_ref, entry_limit=entry_limit)

    mocker.patch("filelock._soft._strict._scan._list_claim_names", side_effect=break_before_scan, autospec=True)
    lock = StrictSoftFileLock(lock_path, timeout=0)

    with pytest.raises(Timeout):
        lock.acquire()
    assert (lock.claims, lock.is_locked) == ((), False)


def test_strict_soft_force_break_held_during_doorway_backs_off(tmp_path: Path, mocker: MockerFixture) -> None:
    lock_path = tmp_path / "resource.lock"
    initialize_protocol(lock_path)
    claims = Path(f"{lock_path}.filelock", "claims")
    real_list = _list_claim_names
    scans = 0

    def break_held_before_final_scan(
        lock_file: str, directory_ref: tuple[str, int | None], *, entry_limit: int | None
    ) -> list[str]:
        nonlocal scans
        scans += 1
        if scans == 3:
            for claim in claims.glob("held-*.claim"):
                claim.unlink()
        return real_list(lock_file, directory_ref, entry_limit=entry_limit)

    mocker.patch(
        "filelock._soft._strict._scan._list_claim_names", side_effect=break_held_before_final_scan, autospec=True
    )
    lock = StrictSoftFileLock(lock_path, timeout=0)

    with pytest.raises(Timeout):
        lock.acquire()
    assert (lock.claims, lock.is_locked) == ((), False)


@NEEDS_DIR_FD  # pragma: needs dir-fd
def test_strict_soft_reaper_removes_private_before_publication(tmp_path: Path, mocker: MockerFixture) -> None:
    lock_path = tmp_path / "resource.lock"
    initialize_protocol(lock_path)
    token = "0" * 32
    private_path = private_claim_path(lock_path, token)
    _reap_before_publication(lock_path, token, mocker)
    lock = StrictSoftFileLock(lock_path, timeout=0)

    with pytest.raises(Timeout):
        lock.acquire()
    assert (lock.claims, lock.is_locked, private_path.exists()) == ((), False, False)


def _reap_before_publication(lock_path: Path, token: str, mocker: MockerFixture) -> None:  # pragma: needs dir-fd
    real_link = os.link
    peer_scanned = False

    def reap_before_link(
        source: PathValue,
        destination: PathValue,
        *,
        src_dir_fd: int | None = None,
        dst_dir_fd: int | None = None,
        follow_symlinks: bool = True,
    ) -> None:
        nonlocal peer_scanned
        # A peer's scan ages out the private record before this publisher links it; the peer's own links pass.
        if Path(os.fsdecode(destination)).name.startswith("intent-") and not peer_scanned:
            peer_scanned = True
            os.utime(private_claim_path(lock_path, token), (0, 0))
            with StrictSoftFileLock(lock_path, timeout=0):
                pass
        real_link(source, destination, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd, follow_symlinks=follow_symlinks)

    real_token_hex = secrets.token_hex
    mocker.patch(
        "secrets.token_hex",
        autospec=True,
        side_effect=lambda nbytes: real_token_hex(nbytes) if peer_scanned else token,
    )
    mocker.patch("os.link", autospec=True, side_effect=reap_before_link)


@CLAIM_LINK_ONLY  # pragma: win32 no cover
def test_strict_soft_reaper_removes_private_after_publication(tmp_path: Path, mocker: MockerFixture) -> None:
    lock_path = tmp_path / "resource.lock"
    initialize_protocol(lock_path)
    token = "0" * 32
    private_path = private_claim_path(lock_path, token)
    real_link = os.link

    def reap_after_link(
        source: PathValue,
        destination: PathValue,
        *,
        src_dir_fd: int | None = None,
        dst_dir_fd: int | None = None,
        follow_symlinks: bool = True,
    ) -> None:
        real_link(
            source,
            destination,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
            follow_symlinks=follow_symlinks,
        )
        if Path(os.fsdecode(destination)).name.startswith("intent-"):
            assert len(StrictSoftFileLock(lock_path).claims) == 1

    mocker.patch("secrets.token_hex", return_value=token, autospec=True)
    mocker.patch("os.link", side_effect=reap_after_link, autospec=True)

    with StrictSoftFileLock(lock_path, timeout=0) as lock:
        assert (lock.claims[0].state, private_path.exists()) == ("held", False)


@NEEDS_DIR_FD  # pragma: needs dir-fd
def test_strict_soft_reaper_replacement_only_aborts_publisher(tmp_path: Path, mocker: MockerFixture) -> None:
    lock_path = tmp_path / "resource.lock"
    initialize_protocol(lock_path)
    token = "0" * 32
    private_path = private_claim_path(lock_path, token)
    displaced_path = private_path.with_suffix(".displaced")
    real_unlink = os.unlink
    replaced = False

    def replace_instead_of_unlink(path: PathValue, *, dir_fd: int | None = None) -> None:
        # The reaper's clock probe and the peer's own records are the other os.unlink callers; let them through.
        nonlocal replaced
        if os.fsdecode(path) != private_path.name:
            real_unlink(path, dir_fd=dir_fd)
            return
        replaced = True
        private_path.replace(displaced_path)
        private_path.write_bytes(b"replacement publisher")

    _reap_before_publication(lock_path, token, mocker)
    mocker.patch("os.unlink", autospec=True, side_effect=replace_instead_of_unlink)
    lock = StrictSoftFileLock(lock_path, timeout=0)

    with pytest.raises(Timeout):
        lock.acquire()
    assert (
        replaced,
        lock.is_locked,
        lock.claims,
        (private_path.parent / f"intent-v1-{token}.claim").exists(),
        private_path.read_bytes(),
        displaced_path.exists(),
    ) == (
        True,
        False,
        (),
        False,
        b"replacement publisher",
        True,
    )
