from __future__ import annotations

import os
import re
import stat
import sys
from errno import EACCES, EIO
from pathlib import Path
from typing import TYPE_CHECKING, Final

import pytest

from filelock import SoftFileLockProtocolError, StrictSoftFileLock, Timeout
from filelock._soft._strict._claims import _PRIVATE_RECORD_MARKER
from filelock._soft._strict._directories import scandir_relative
from tests.capability_marks import (
    NEEDS_FOREIGN_STICKY_TMP,
    NEEDS_SYMLINK,
    NEEDS_USER_NAMESPACES,
)

if TYPE_CHECKING:
    from collections.abc import Iterator
    from contextlib import AbstractContextManager

    from pytest_mock import MockerFixture

    from .filesystem_faults import PathValue
from .capability_marks import NEEDS_DIR_FD
from .claim_records import write_held_claim

pytestmark: Final[pytest.MarkDecorator] = pytest.mark.requires_hard_links


def test_strict_soft_directory_inspection_error_fails_closed(tmp_path: Path, mocker: MockerFixture) -> None:
    real_mkdir = Path.mkdir

    def fail_protocol_mkdir(path: Path, mode: int = 0o777, parents: bool = False, exist_ok: bool = False) -> None:
        if str(path).endswith(".filelock"):
            raise FileExistsError
        real_mkdir(path, mode=mode, parents=parents, exist_ok=exist_ok)

    mocker.patch.object(Path, "mkdir", autospec=True, side_effect=fail_protocol_mkdir)
    mocker.patch.object(Path, "lstat", autospec=True, side_effect=PermissionError(EACCES, "cannot inspect"))

    with pytest.raises(SoftFileLockProtocolError, match="cannot inspect"):
        StrictSoftFileLock(tmp_path / "resource.lock").acquire()


@NEEDS_DIR_FD  # pragma: needs dir-fd
@NEEDS_SYMLINK  # pragma: needs symlink
@pytest.mark.parametrize(
    ("function", "suffix", "occurrence", "match"),
    [
        pytest.param("mkdir", "claims", 1, "cannot open claim directory", id="before-creating-claims"),
        pytest.param("open", ".filelock", 1, "cannot open .*filelock$", id="before-settling-the-mode"),
        pytest.param("open", ".filelock", 2, "cannot open .*filelock$", id="before-opening-coordination"),
    ],
)
def test_strict_soft_coordination_directory_swapped_for_symlink_leaves_target_alone(
    tmp_path: Path, mocker: MockerFixture, function: str, suffix: str, occurrence: int, match: str
) -> None:
    lock_path: Final[Path] = tmp_path / "resource.lock"
    (coordination := Path(f"{lock_path}.filelock")).mkdir()
    (target := tmp_path / "target").mkdir()
    real = getattr(os, function)
    matches = 0

    def swap_at_the_occurrence(path: PathValue, *args: int, **options: int | None) -> int | None:
        # A process that can rename entries in the lock's directory can swap the name after the lock saw a directory.
        nonlocal matches
        matches += os.fsdecode(path).endswith(suffix)
        if matches == occurrence and not coordination.is_symlink():
            coordination.rename(tmp_path / "aside")
            coordination.symlink_to(target, target_is_directory=True)
        return real(path, *args, **options)

    mocker.patch(f"os.{function}", autospec=True, side_effect=swap_at_the_occurrence)

    with pytest.raises(SoftFileLockProtocolError, match=match):
        StrictSoftFileLock(lock_path, timeout=0).acquire()
    assert list(target.iterdir()) == []


@NEEDS_DIR_FD  # pragma: needs dir-fd
@NEEDS_SYMLINK  # pragma: needs symlink
def test_strict_soft_release_keeps_claims_while_coordination_directory_is_a_symlink(
    initialized_lock_path: Path, tmp_path: Path
) -> None:
    coordination, aside, target = Path(f"{initialized_lock_path}.filelock"), tmp_path / "aside", tmp_path / "target"
    (target / "claims").mkdir(parents=True)
    lock = StrictSoftFileLock(initialized_lock_path)
    lock.acquire()
    coordination.rename(aside)
    coordination.symlink_to(target, target_is_directory=True)

    # Following the symlink would find no claim in the target, count both as removed and strand them.
    with pytest.raises(BaseExceptionGroup, match="strict claim release failed"):
        lock.release()
    assert (lock.is_locked, len(list((aside / "claims").iterdir()))) == (True, 2)

    coordination.unlink()
    aside.rename(coordination)
    lock.release()
    assert (lock.is_locked, lock.claims) == (False, ())


@NEEDS_DIR_FD  # pragma: needs dir-fd
@NEEDS_SYMLINK  # pragma: needs symlink
def test_strict_soft_symlinked_claims_directory_is_neither_listed_nor_broken(tmp_path: Path) -> None:
    lock_path = tmp_path / "resource.lock"
    claim = write_held_claim(tmp_path / "target" / "claims")
    Path(f"{lock_path}.filelock").mkdir()
    Path(f"{lock_path}.filelock", "claims").symlink_to(claim.parent, target_is_directory=True)
    lock = StrictSoftFileLock(lock_path)

    with pytest.raises(SoftFileLockProtocolError, match="cannot open claim directory"):
        _ = lock.claims
    with pytest.raises(OSError, match=re.escape("claims'")):
        lock.force_break(claim.name)
    assert claim.exists()


@NEEDS_DIR_FD  # pragma: needs dir-fd
@NEEDS_SYMLINK  # pragma: needs symlink
def test_strict_soft_symlinked_protocol_directory_is_neither_listed_nor_broken(tmp_path: Path) -> None:
    lock_path = tmp_path / "resource.lock"
    claim = write_held_claim(tmp_path / "target" / "claims")
    Path(f"{lock_path}.filelock").symlink_to(claim.parent.parent, target_is_directory=True)
    lock = StrictSoftFileLock(lock_path)

    with pytest.raises(SoftFileLockProtocolError, match="cannot open claim directory"):
        _ = lock.claims
    with pytest.raises(OSError, match=re.escape("resource.lock.filelock'")):
        lock.force_break(claim.name)
    assert claim.exists()


@NEEDS_FOREIGN_STICKY_TMP
@pytest.mark.usefixtures("directories_of_another_account")
def test_strict_soft_refuses_coordination_directory_of_another_account(  # pragma: needs foreign-sticky-tmp
    lock_path_in_shared_tmp: Path,
) -> None:
    (coordination := Path(f"{lock_path_in_shared_tmp}.filelock")).mkdir()

    with pytest.raises(SoftFileLockProtocolError, match="not to this process's account"):
        StrictSoftFileLock(lock_path_in_shared_tmp, timeout=0).acquire()
    assert list(coordination.iterdir()) == []


@NEEDS_FOREIGN_STICKY_TMP
def test_strict_soft_refuses_sentinel_of_another_account(  # pragma: needs foreign-sticky-tmp
    lock_path_in_shared_tmp: Path,
    mocker: MockerFixture,
) -> None:
    sentinel = b"1\nfilelock-strict-v1\x00\n0\n"
    lock_path_in_shared_tmp.write_bytes(sentinel)
    sentinel_stat = lock_path_in_shared_tmp.stat()
    fstat = os.fstat

    def owner_of_fd(fd: int) -> os.stat_result:
        info = fstat(fd)
        if os.path.samestat(info, sentinel_stat):
            return os.stat_result((*info[:4], info.st_uid + 1, *info[5:]))
        return info

    mocker.patch.object(os, "fstat", autospec=True, side_effect=owner_of_fd)
    lock = StrictSoftFileLock(lock_path_in_shared_tmp, timeout=0)

    try:
        with pytest.raises(SoftFileLockProtocolError, match="sentinel belongs to uid"):
            lock.acquire()
    finally:
        lock.release(force=True)
    assert lock_path_in_shared_tmp.read_bytes() == sentinel


@pytest.mark.skipif(sys.platform == "win32", reason="Windows has no directory mode to settle")
def test_strict_soft_refuses_unrelated_directory_at_coordination_name(tmp_path: Path) -> None:  # pragma: win32 no cover
    lock_path: Final[Path] = tmp_path / "resource.lock"
    (site := tmp_path / "site").mkdir()
    (site / "index.html").write_text("page", encoding="utf-8")
    site.chmod(0o755)
    site.rename(coordination := Path(f"{lock_path}.filelock"))

    with pytest.raises(SoftFileLockProtocolError, match="holds entries that are not this lock's"):
        StrictSoftFileLock(lock_path, mode=0o600, timeout=0).acquire()
    assert (stat.S_IMODE(coordination.stat().st_mode), sorted(path.name for path in coordination.iterdir())) == (
        0o755,
        ["index.html"],
    )


@pytest.mark.skipif(sys.platform == "win32", reason="Windows has no directory mode to settle")
def test_strict_soft_refuses_file_at_claim_name_before_coordination_chmod(  # pragma: win32 no cover
    tmp_path: Path,
) -> None:
    lock_path = tmp_path / "resource.lock"
    (coordination := Path(f"{lock_path}.filelock")).mkdir()
    (coordination / "claims").write_text("unrelated", encoding="utf-8")
    coordination.chmod(0o755)

    with pytest.raises(SoftFileLockProtocolError, match="holds entries that are not this lock's"):
        StrictSoftFileLock(lock_path, mode=0o600, timeout=0).acquire()
    assert (stat.S_IMODE(coordination.stat().st_mode), (coordination / "claims").read_text(encoding="utf-8")) == (
        0o755,
        "unrelated",
    )


@pytest.mark.skipif(sys.platform == "win32", reason="Windows has no directory mode to settle")
def test_strict_soft_refuses_unrelated_claim_before_coordination_chmod(  # pragma: win32 no cover
    tmp_path: Path,
) -> None:
    lock_path = tmp_path / "resource.lock"
    (claims := (coordination := Path(f"{lock_path}.filelock")) / "claims").mkdir(parents=True)
    (claims / ".gitkeep").touch()
    coordination.chmod(0o755)
    claims.chmod(0o1700)

    with pytest.raises(SoftFileLockProtocolError, match="unknown claim name"):
        StrictSoftFileLock(lock_path, mode=0o600, timeout=0).acquire()
    assert (stat.S_IMODE(coordination.stat().st_mode), sorted(path.name for path in claims.iterdir())) == (
        0o755,
        [".gitkeep"],
    )


@pytest.mark.skipif(sys.platform == "win32", reason="Windows does not inspect directory mode")
def test_strict_soft_refuses_unlistable_coordination_directory(  # pragma: win32 no cover
    tmp_path: Path, mocker: MockerFixture
) -> None:
    lock_path = tmp_path / "resource.lock"
    mocker.patch(
        "filelock._soft._strict._directories.scandir_relative",
        autospec=True,
        side_effect=PermissionError(EACCES, "denied"),
    )

    with pytest.raises(SoftFileLockProtocolError, match="cannot list"):
        StrictSoftFileLock(lock_path, timeout=0).acquire()
    assert not (Path(f"{lock_path}.filelock") / "claims").exists()


@pytest.mark.skipif(sys.platform == "win32", reason="Windows does not inspect directory mode")
def test_strict_soft_refuses_uninspectable_directory_mode(  # pragma: win32 no cover
    tmp_path: Path, mocker: MockerFixture
) -> None:
    real_fstat = os.fstat

    def fail_directory_fstat(fd: int) -> os.stat_result:
        if stat.S_ISDIR(real_fstat(fd).st_mode):
            raise OSError(EIO, "uninspectable")
        return real_fstat(fd)

    mocker.patch("os.fstat", autospec=True, side_effect=fail_directory_fstat)
    lock_path = tmp_path / "resource.lock"

    with pytest.raises(SoftFileLockProtocolError, match="cannot set the mode"):
        StrictSoftFileLock(lock_path, timeout=0).acquire()
    assert not (Path(f"{lock_path}.filelock") / "claims").exists()


@pytest.mark.skipif(sys.platform == "win32", reason="Windows does not inspect directory mode")
def test_strict_soft_refuses_unopenable_nested_claim_directory(  # pragma: win32 no cover
    tmp_path: Path, mocker: MockerFixture
) -> None:
    lock_path = tmp_path / "resource.lock"
    (coordination := Path(f"{lock_path}.filelock")).mkdir()
    (coordination / "claims").mkdir()
    coordination.chmod(0o755)
    mocker.patch(
        "filelock._soft._strict._directories.open_directory",
        autospec=True,
        side_effect=PermissionError(EACCES, "denied"),
    )

    with pytest.raises(SoftFileLockProtocolError, match="cannot open"):
        StrictSoftFileLock(lock_path, mode=0o600, timeout=0).acquire()
    assert stat.S_IMODE(coordination.stat().st_mode) == 0o755


@pytest.mark.skipif(sys.platform == "win32", reason="Windows has no directory mode to settle")
@pytest.mark.parametrize("entry", ["run-1.csv", ".gitkeep", ".nfsgarbage"])
@pytest.mark.parametrize("mode", [0o775, 0o1700])
def test_strict_soft_refuses_unrelated_entry_in_claim_directory(  # pragma: win32 no cover
    tmp_path: Path, entry: str, mode: int
) -> None:
    lock_path: Final[Path] = tmp_path / "resource.lock"
    (claims := Path(f"{lock_path}.filelock") / "claims").mkdir(parents=True)
    (claims / entry).touch()
    claims.chmod(mode)

    with pytest.raises(SoftFileLockProtocolError, match="unknown claim name"):
        StrictSoftFileLock(lock_path, mode=0o600, timeout=0).acquire()
    assert (stat.S_IMODE(claims.stat().st_mode), sorted(path.name for path in claims.iterdir())) == (mode, [entry])


@pytest.mark.skipif(sys.platform == "win32", reason="Windows has no directory mode to settle")
def test_strict_soft_accepts_nfs_unlinked_open_claim(tmp_path: Path) -> None:  # pragma: win32 no cover
    lock_path = tmp_path / "resource.lock"
    (claims := Path(f"{lock_path}.filelock") / "claims").mkdir(parents=True)
    (silly_rename := claims / ".nfs00000000004c0019000002e4").touch()

    with StrictSoftFileLock(lock_path, mode=0o600, timeout=0):
        assert (silly_rename.exists(), stat.S_IMODE(claims.stat().st_mode)) == (True, 0o1700)


@pytest.mark.skipif(sys.platform == "win32", reason="Windows does not inspect directory mode")
def test_strict_soft_refuses_unlistable_claim_directory_before_chmod(  # pragma: win32 no cover
    tmp_path: Path, mocker: MockerFixture
) -> None:
    lock_path = tmp_path / "resource.lock"
    (claims := Path(f"{lock_path}.filelock") / "claims").mkdir(parents=True)
    claims.chmod(0o775)
    real_scandir = scandir_relative

    def fail_claim_scan(
        directory_ref: tuple[str, int | None],
    ) -> AbstractContextManager[Iterator[os.DirEntry[str]]]:
        if directory_ref[0] == os.fspath(claims):
            raise PermissionError(EACCES, "denied")
        return real_scandir(directory_ref)

    mocker.patch("filelock._soft._strict._directories.scandir_relative", autospec=True, side_effect=fail_claim_scan)

    with pytest.raises(SoftFileLockProtocolError, match="cannot list"):
        StrictSoftFileLock(lock_path, mode=0o600, timeout=0).acquire()
    assert stat.S_IMODE(claims.stat().st_mode) == 0o775


def test_strict_soft_claims_refuses_unknown_public_name(tmp_path: Path) -> None:
    lock_path = tmp_path / "resource.lock"
    (claims := Path(f"{lock_path}.filelock") / "claims").mkdir(parents=True)
    (claims / "held-v2-claim").touch()

    with pytest.raises(SoftFileLockProtocolError, match="unknown claim name"):
        _ = StrictSoftFileLock(lock_path).claims


@pytest.mark.skipif(sys.platform == "win32", reason="Windows has no directory mode to settle")
@pytest.mark.parametrize(
    "entry",
    [
        pytest.param(f".held-v1-{'0' * 32}.claim{_PRIVATE_RECORD_MARKER}{'0' * 32}.tmp", id="private-record"),
        pytest.param(f".probe-v1-{'0' * 32}.tmp", id="claim-probe"),
    ],
)
def test_strict_soft_narrows_claim_directory_with_private_records(  # pragma: win32 no cover
    tmp_path: Path, entry: str
) -> None:
    lock_path: Final[Path] = tmp_path / "resource.lock"
    (claims := Path(f"{lock_path}.filelock") / "claims").mkdir(parents=True)
    (claims / entry).touch()
    claims.chmod(0o777)

    with StrictSoftFileLock(lock_path, mode=0o600, timeout=0):
        assert stat.S_IMODE(claims.stat().st_mode) == 0o1700


@pytest.mark.skipif(sys.platform == "win32", reason="Windows has no directory mode to settle")
def test_strict_soft_narrows_claim_directory_with_live_claim(tmp_path: Path) -> None:  # pragma: win32 no cover
    lock_path: Final[Path] = tmp_path / "resource.lock"
    with StrictSoftFileLock(lock_path):
        (claims := Path(f"{lock_path}.filelock") / "claims").chmod(0o777)
        with pytest.raises(Timeout):
            StrictSoftFileLock(lock_path, mode=0o600, timeout=0).acquire()
        assert stat.S_IMODE(claims.stat().st_mode) == 0o1700


@NEEDS_FOREIGN_STICKY_TMP
@NEEDS_USER_NAMESPACES  # pragma: darwin no cover
@pytest.mark.usefixtures("uid_shared_with_unmapped_accounts")
def test_strict_soft_refuses_coordination_directory_with_an_unmapped_owner(  # pragma: needs foreign-sticky-tmp
    lock_path_in_shared_tmp: Path,
) -> None:
    (coordination := Path(f"{lock_path_in_shared_tmp}.filelock")).mkdir()

    with pytest.raises(SoftFileLockProtocolError, match="not to this process's account"):
        StrictSoftFileLock(lock_path_in_shared_tmp, timeout=0).acquire()
    assert list(coordination.iterdir()) == []


@NEEDS_FOREIGN_STICKY_TMP
@NEEDS_USER_NAMESPACES  # pragma: darwin no cover
@pytest.mark.parametrize(
    "error",
    [pytest.param(OSError("proc unavailable"), id="unreadable"), pytest.param(ValueError("invalid"), id="invalid")],
)
def test_strict_soft_refuses_ownership_when_overflow_uid_is_unknown(  # pragma: needs foreign-sticky-tmp
    lock_path_in_shared_tmp: Path, mocker: MockerFixture, error: OSError | ValueError
) -> None:
    (coordination := Path(f"{lock_path_in_shared_tmp}.filelock")).mkdir()
    mocker.patch("filelock._filesystem._ownership._linux_owner_probe", autospec=True, return_value=None)
    mocker.patch.object(Path, "read_text", autospec=True, side_effect=error)
    mocker.patch.object(Path, "read_bytes", autospec=True, side_effect=OSError("no UID map"))

    with pytest.raises(SoftFileLockProtocolError, match="not to this process's account"):
        StrictSoftFileLock(lock_path_in_shared_tmp, timeout=0).acquire()
    assert list(coordination.iterdir()) == []


@pytest.mark.skipif(
    sys.platform == "win32", reason="Windows pins the coordination directory throughout a hold"
)  # pragma: win32 no cover
def test_strict_soft_coordination_directory_replaced_in_a_writable_parent_splits_holders(tmp_path: Path) -> None:
    # A shared, non-sticky parent lets another process rename the protocol directory during a hold.
    lock_path: Final[Path] = tmp_path / "resource.lock"
    (first := StrictSoftFileLock(lock_path)).acquire()
    Path(f"{lock_path}.filelock").rename(tmp_path / "aside")
    (second := StrictSoftFileLock(lock_path, timeout=0)).acquire()

    assert (first.is_locked, second.is_locked) == (True, True)
