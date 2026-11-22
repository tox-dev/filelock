from __future__ import annotations

import contextlib
import os
import stat
import sys
from errno import EIO
from pathlib import Path
from typing import TYPE_CHECKING, Final

import pytest

from filelock import SoftFileLockProtocolError, StrictSoftFileLock, Timeout
from filelock._soft._strict._claims import _PRIVATE_RECORD_MARKER
from filelock._soft._strict._directories import LIMITS
from filelock._soft._strict._lock import open_or_create_sentinel
from filelock._soft._strict._scan import scandir_relative

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from contextlib import AbstractContextManager

    from pytest_mock import MockerFixture
from .claim_records import private_claim_path, write_foreign_claim

pytestmark: Final[pytest.MarkDecorator] = pytest.mark.requires_hard_links


def test_strict_soft_claim_flood_counts_as_contention(initialized_lock_path: Path) -> None:
    claims = Path(f"{initialized_lock_path}.filelock") / "claims"
    for index in range(1024):
        write_foreign_claim(claims, f"{index:032x}")
    (claims / f"held-v1-{'f' * 32}.claim").write_bytes(b"broken")

    lock = StrictSoftFileLock(initialized_lock_path, blocking=False)
    with pytest.raises(Timeout):
        lock.acquire()
    assert (lock.is_locked, lock.lock_counter) == (False, 0)


@pytest.mark.parametrize(
    ("last_name", "error"),
    [
        pytest.param(f"held-v1-{'f' * 32}.claim", Timeout, id="claims"),
        pytest.param("unknown", SoftFileLockProtocolError, id="invalid"),
    ],
)
def test_strict_soft_claim_flood_after_preflight(
    initialized_lock_path: Path, mocker: MockerFixture, last_name: str, error: type[Exception]
) -> None:
    mocker.patch.object(type(LIMITS), "claims", 2)
    claims = Path(f"{initialized_lock_path}.filelock") / "claims"
    open_sentinel = open_or_create_sentinel

    def plant_after_preflight(
        lock_file: str, path: Path, mode: int, unchanged_for: Callable[[os.stat_result], float]
    ) -> int | None:
        write_foreign_claim(claims, "0" * 32)
        write_foreign_claim(claims, "1" * 32)
        (claims / last_name).write_bytes(b"broken")
        return open_sentinel(lock_file, path, mode, unchanged_for)

    mocker.patch(
        "filelock._soft._strict._lock.open_or_create_sentinel", autospec=True, side_effect=plant_after_preflight
    )
    lock = StrictSoftFileLock(initialized_lock_path, blocking=False)
    with pytest.raises(error):
        lock.acquire()
    assert (lock.is_locked, lock.lock_counter) == (False, 0)


def test_strict_soft_claim_inspection_lists_past_scan_limit(initialized_lock_path: Path, mocker: MockerFixture) -> None:
    mocker.patch.object(type(LIMITS), "claims", 2)
    claims = Path(f"{initialized_lock_path}.filelock") / "claims"
    for index in range(3):
        write_foreign_claim(claims, f"{index:032x}")

    assert len(StrictSoftFileLock(initialized_lock_path).claims) == 3


def test_strict_soft_claim_scan_skips_private_probes(initialized_lock_path: Path, mocker: MockerFixture) -> None:
    mocker.patch.object(type(LIMITS), "claims", 2)
    claims = Path(f"{initialized_lock_path}.filelock") / "claims"
    for index in range(3):
        (claims / f".probe-v1-{index:032x}.tmp").touch()

    with StrictSoftFileLock(initialized_lock_path, blocking=False) as lock:
        assert lock.is_locked


@pytest.mark.parametrize("extra", [pytest.param(1, id="reserved-slots"), pytest.param(5, id="total-limit")])
def test_strict_soft_claim_directory_reserves_space_before_publication(
    initialized_lock_path: Path, mocker: MockerFixture, extra: int
) -> None:
    claims = Path(f"{initialized_lock_path}.filelock") / "claims"
    mocker.patch.object(type(LIMITS), "entries", 7)
    for index in range(3):
        (claims / f".nfs{index:04x}").touch()
    lock = StrictSoftFileLock(initialized_lock_path, blocking=False)
    with lock:
        assert lock.is_locked

    for index in range(3, 3 + extra):
        (claims / f".nfs{index:04x}").touch()
    with pytest.raises(SoftFileLockProtocolError, match="exceeds the scan limit"):
        lock.acquire()
    assert (lock.is_locked, list(claims.glob("*.claim"))) == (False, [])


def test_strict_soft_claim_directory_growing_after_publication_withdraws(
    initialized_lock_path: Path, mocker: MockerFixture
) -> None:
    claims = Path(f"{initialized_lock_path}.filelock") / "claims"
    mocker.patch.object(type(LIMITS), "entries", 4)
    real_scandir = scandir_relative

    def grow_after_intent(directory_ref: tuple[str, int | None]) -> AbstractContextManager[Iterator[os.DirEntry[str]]]:
        if any(claims.glob("intent-*.claim")):
            for index in range(4):
                (claims / f".nfs{index:04x}").touch()
        return real_scandir(directory_ref)

    mocker.patch("filelock._soft._strict._scan.scandir_relative", autospec=True, side_effect=grow_after_intent)
    lock = StrictSoftFileLock(initialized_lock_path, blocking=False)
    with pytest.raises(Timeout):
        lock.acquire()
    assert (lock.is_locked, list(claims.glob("*.claim"))) == (False, [])


def test_strict_soft_first_scan_names_overfull_directory(initialized_lock_path: Path, mocker: MockerFixture) -> None:
    claims = Path(f"{initialized_lock_path}.filelock") / "claims"
    open_sentinel = open_or_create_sentinel

    def add_probes(
        lock_file: str, path: Path, mode: int, unchanged_for: Callable[[os.stat_result], float]
    ) -> int | None:
        for index in range(3):
            (claims / f".probe-v1-{index:032x}.tmp").touch()
        return open_sentinel(lock_file, path, mode, unchanged_for)

    mocker.patch.object(type(LIMITS), "entries", 6)
    mocker.patch("filelock._soft._strict._lock.open_or_create_sentinel", autospec=True, side_effect=add_probes)
    with pytest.raises(SoftFileLockProtocolError, match="2 hidden entries") as caught:
        StrictSoftFileLock(initialized_lock_path, blocking=False).acquire()
    assert str(claims) in str(caught.value)


@pytest.mark.skipif(sys.platform == "win32", reason="Windows has no directory mode to settle")
def test_strict_soft_claim_scan_does_not_chmod_past_limit(  # pragma: win32 no cover
    initialized_lock_path: Path, mocker: MockerFixture
) -> None:
    mocker.patch.object(type(LIMITS), "claims", 2)
    claims = Path(f"{initialized_lock_path}.filelock") / "claims"
    for index in range(3):
        write_foreign_claim(claims, f"{index:032x}")
    nfs = claims / ".nfs0123"
    nfs.touch()
    claims.chmod(0o777)

    with pytest.raises(Timeout):
        StrictSoftFileLock(initialized_lock_path, mode=0o600, blocking=False).acquire()
    assert (stat.S_IMODE(claims.stat().st_mode), nfs.exists()) == (0o777, True)


def test_strict_soft_claim_scan_leaves_private_records_on_overflow(
    initialized_lock_path: Path, mocker: MockerFixture
) -> None:
    mocker.patch.object(type(LIMITS), "claims", 2)
    claims = Path(f"{initialized_lock_path}.filelock") / "claims"
    for index in range(3):
        private_path = private_claim_path(initialized_lock_path, f"{index:032x}")
        private_path.write_bytes(b"abandoned")
        os.utime(private_path, (0, 0))
    write_foreign_claim(claims, "f" * 32)

    with pytest.raises(Timeout):
        StrictSoftFileLock(initialized_lock_path, blocking=False).acquire()
    assert any(path.name.startswith(".") for path in claims.iterdir())


@pytest.mark.skipif(sys.platform == "win32", reason="Windows does not scan claim names during directory setup")
def test_strict_soft_claim_scan_recovers_private_records_in_one_attempt(  # pragma: win32 no cover
    initialized_lock_path: Path, mocker: MockerFixture
) -> None:
    mocker.patch.object(type(LIMITS), "claims", 2)
    claims = Path(f"{initialized_lock_path}.filelock") / "claims"
    for index in range(3):
        private_path = private_claim_path(initialized_lock_path, f"{index:032x}")
        private_path.write_bytes(b"abandoned")
        os.utime(private_path, (0, 0))

    with StrictSoftFileLock(initialized_lock_path, blocking=False) as lock:
        assert lock.is_locked
    assert not list(claims.iterdir())


@pytest.mark.skipif(sys.platform == "win32", reason="Windows does not scan claim names during directory setup")
def test_strict_soft_claim_scan_reports_private_cleanup_error(  # pragma: win32 no cover
    initialized_lock_path: Path, mocker: MockerFixture
) -> None:
    mocker.patch.object(type(LIMITS), "claims", 2)
    for index in range(3):
        private_path = private_claim_path(initialized_lock_path, f"{index:032x}")
        private_path.write_bytes(b"abandoned")
        os.utime(private_path, (0, 0))
        write_foreign_claim(private_path.parent, f"{index:032x}")
    mocker.patch(
        "filelock._soft._strict._directories.unlink_private_record_once",
        autospec=True,
        side_effect=OSError(EIO, "I/O error"),
    )

    with pytest.raises(SoftFileLockProtocolError, match="cannot reclaim private claim records"):
        StrictSoftFileLock(initialized_lock_path, blocking=False).acquire()


def test_strict_soft_sentinel_scan_reclaims_at_most_one_batch(tmp_path: Path, mocker: MockerFixture) -> None:
    mocker.patch.object(type(LIMITS), "claims", 2)
    lock_path = tmp_path / "resource.lock"
    for index in range(3):
        private_path = tmp_path / f".{lock_path.name}{_PRIVATE_RECORD_MARKER}{index:032x}.tmp"
        private_path.write_bytes(b"abandoned")
        os.utime(private_path, (0, 0))

    with StrictSoftFileLock(lock_path, blocking=False):
        assert any(path.name.startswith(".") for path in tmp_path.iterdir())


@pytest.mark.skipif(sys.platform == "win32", reason="Windows can leave private records until operator cleanup")
def test_strict_soft_sentinel_scan_skips_unrelated_entries(  # pragma: win32 no cover
    tmp_path: Path, mocker: MockerFixture
) -> None:
    mocker.patch.object(type(LIMITS), "claims", 2)
    lock_path = tmp_path / "resource.lock"
    for index in range(3):
        (tmp_path / f"unrelated-{index}").touch()
    private_path = tmp_path / f".{lock_path.name}{_PRIVATE_RECORD_MARKER}{'a' * 32}.tmp"
    private_path.touch()
    os.utime(private_path, (0, 0))
    real_scandir = os.scandir

    def unrelated_first(path: int | str | Path) -> AbstractContextManager[Iterator[os.DirEntry[str]]]:
        with real_scandir(path) as entries:
            return contextlib.nullcontext(iter(sorted(entries, key=lambda entry: entry.name, reverse=True)))

    mocker.patch("os.scandir", autospec=True, side_effect=unrelated_first)
    with StrictSoftFileLock(lock_path, blocking=False):
        assert not private_path.exists()
