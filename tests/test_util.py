from __future__ import annotations

import os
import runpy
import socket
import sys
import time
from contextlib import suppress
from errno import EACCES, EIO, ENOENT, EPERM
from pathlib import Path
from typing import TYPE_CHECKING, Final

import pytest

from filelock import SoftFileLock, Timeout

if TYPE_CHECKING:
    from collections.abc import Callable, Generator

    from pytest_mock import MockerFixture

_SKIP_AS_ROOT: Final[pytest.MarkDecorator] = pytest.mark.skipif(
    sys.platform != "win32" and os.geteuid() == 0,
    reason="root can write a 0o444 file",
)
# Linux and macOS cap PIDs below 2**22 + 1, and Windows answers OpenProcess for it with an invalid-parameter error, so
# the marker names a holder every platform reads as dead.
_DEAD_HOLDER: Final[str] = f"{2**22 + 1}\n{socket.gethostname()}\n"


@pytest.fixture
def stale_lock(tmp_path: Path) -> Path:
    path: Final[Path] = tmp_path / "test.lock"
    path.write_text(_DEAD_HOLDER, encoding="utf-8")
    return path


def test_break_lock_file_unlinks_a_dead_holders_marker(stale_lock: Path) -> None:
    with SoftFileLock(stale_lock, timeout=1):
        assert list(stale_lock.parent.glob("test.lock.break.*")) == []


def _rewrite_in_place(lock: Path) -> None:
    lock.write_text("live", encoding="utf-8")
    os.utime(lock, (time.time() + 10, time.time() + 10))


def _replace_with_same_mtime(lock: Path) -> None:
    # A filesystem with coarse modification times (NFS, FAT) gives a same-second recreation the old mtime, so only the
    # inode tells it apart. Writing the replacement while the original exists guarantees a fresh inode.
    stale: Final[os.stat_result] = lock.stat()
    (replacement := lock.with_name("recreated")).write_text("live", encoding="utf-8")
    os.utime(replacement, ns=(stale.st_atime_ns, stale.st_mtime_ns))
    replacement.replace(lock)


@pytest.mark.parametrize(
    "recreate",
    [pytest.param(_rewrite_in_place, id="mtime-advanced"), pytest.param(_replace_with_same_mtime, id="inode-changed")],
)
def test_break_lock_file_leaves_a_recreated_marker_aside(
    stale_lock: Path, mocker: MockerFixture, recreate: Callable[[Path], None]
) -> None:
    rename: Final = Path.rename

    def peer_recreates_then_rename(source: Path, target: str) -> Path:
        recreate(source)
        return rename(source, target)

    mocker.patch.object(Path, "rename", autospec=True, side_effect=peer_recreates_then_rename)
    with pytest.raises(Timeout):
        SoftFileLock(stale_lock).acquire(blocking=False)

    # The live holder needs its marker, so it survives under the break name and the lock path stays empty.
    assert (
        [path.read_text(encoding="utf-8") for path in stale_lock.parent.glob("test.lock.break.*")],
        stale_lock.exists(),
    ) == (["live"], False)


def test_break_lock_file_lets_acquire_retry_after_the_marker_vanishes(stale_lock: Path, mocker: MockerFixture) -> None:
    rename: Final = Path.rename

    def holder_releases_then_rename(source: Path, target: str) -> Path:
        source.unlink()
        return rename(source, target)

    mocker.patch.object(Path, "rename", autospec=True, side_effect=holder_releases_then_rename)
    with SoftFileLock(stale_lock, timeout=1):
        assert stale_lock.read_text(encoding="utf-8").splitlines()[0] == str(os.getpid())


def test_break_lock_file_break_name_is_unguessable(stale_lock: Path, mocker: MockerFixture) -> None:
    # A second breaker in this process could compute <lock>.break.<pid> and rename a recreated live lock onto it
    # between our lstat and unlink. Were that our break name, our unlink would delete the live lock.
    guessable: Final[Path] = stale_lock.with_name(f"test.lock.break.{os.getpid()}")
    lstat: Final = os.lstat

    def peer_moves_a_live_lock_onto_the_guessable_name(path: str | os.PathLike[str]) -> os.stat_result:
        result: Final[os.stat_result] = lstat(path)
        if ".break." in os.fspath(path) and not guessable.exists():
            stale_lock.write_text("live", encoding="utf-8")
            stale_lock.rename(guessable)
        return result

    mocker.patch("os.lstat", side_effect=peer_moves_a_live_lock_onto_the_guessable_name)
    with pytest.raises(Timeout):
        SoftFileLock(stale_lock).acquire(blocking=False)

    assert guessable.read_text(encoding="utf-8") == "live"


@pytest.mark.skipif(sys.platform == "win32", reason="symlink-to-dir raises IsADirectoryError only on Unix")
def test_writability_check_does_not_follow_symlink_to_dir(tmp_path: Path) -> None:  # pragma: win32 no cover
    (target := tmp_path / "targetdir").mkdir()
    (link := tmp_path / "my.lock").symlink_to(target)
    # Following the symlink would see a directory and raise IsADirectoryError; the lock waits on the link instead.
    with pytest.raises(Timeout):
        SoftFileLock(link).acquire(blocking=False)


@pytest.mark.skipif(sys.platform == "win32", reason="symlink + 0o444 semantics differ on Windows")
@_SKIP_AS_ROOT  # pragma: win32 no cover
def test_writability_check_does_not_follow_symlink_to_readonly(tmp_path: Path) -> None:
    (target := tmp_path / "readonly").write_text("x", encoding="utf-8")
    target.chmod(0o444)
    (link := tmp_path / "my.lock").symlink_to(target)
    # Following the symlink would see a read-only file and raise PermissionError; the link itself is writable.
    with pytest.raises(Timeout):
        SoftFileLock(link).acquire(blocking=False)


@pytest.mark.skipif(sys.platform == "win32", reason="a real directory raises PermissionError on Windows")
@pytest.mark.parametrize("mtime", [pytest.param(0, id="mtime-zero"), pytest.param(2_000_000_000, id="mtime-future")])
def test_writability_check_rejects_a_directory(tmp_path: Path, mtime: int) -> None:  # pragma: win32 no cover
    (path := tmp_path / "a_dir").mkdir()
    os.utime(path, (mtime, mtime))
    with pytest.raises(IsADirectoryError):
        SoftFileLock(path).acquire(blocking=False)


@_SKIP_AS_ROOT
@pytest.mark.parametrize("mtime", [pytest.param(0, id="mtime-zero"), pytest.param(2_000_000_000, id="mtime-future")])
def test_writability_check_rejects_a_readonly_file(tmp_path: Path, mtime: int) -> None:
    (path := tmp_path / "ro.lock").write_text("x", encoding="utf-8")
    path.chmod(0o444)
    try:
        os.utime(path, (mtime, mtime))
        with pytest.raises(PermissionError):
            SoftFileLock(path).acquire(blocking=False)
    finally:
        path.chmod(0o644)


@pytest.mark.parametrize(
    "mode", [pytest.param(0o444, id="acl"), pytest.param(0o464, id="group"), pytest.param(0o446, id="other")]
)
@pytest.mark.parametrize("effective_ids", [pytest.param(True, id="supported"), pytest.param(False, id="unsupported")])
def test_writability_uses_open_credentials(
    readonly_marker: Path, mocker: MockerFixture, mode: int, *, effective_ids: bool
) -> None:
    readonly_marker.chmod(mode)

    def access(filename: str, permission: int, *, effective_ids: bool = False) -> bool:
        assert (filename, permission) == (str(readonly_marker), os.W_OK)
        return effective_ids == supported

    supported: Final = effective_ids
    probe: Final = mocker.patch("os.access", autospec=True, side_effect=access)
    mocker.patch("os.supports_effective_ids", {probe} if effective_ids else set())
    with pytest.raises(Timeout):
        SoftFileLock(readonly_marker).acquire(timeout=0)


def test_writability_allows_creation_after_marker_disappears(readonly_marker: Path, mocker: MockerFixture) -> None:
    def access(filename: str, permission: int, *, effective_ids: bool = False) -> bool:
        assert (permission, effective_ids) == (os.W_OK, os.access in os.supports_effective_ids)
        readonly_marker.chmod(0o644)
        Path(filename).unlink()
        return False

    mocker.patch("os.access", autospec=True, side_effect=access)
    with (lock := SoftFileLock(readonly_marker)).acquire(timeout=0):
        assert lock.is_locked


def test_writability_rejects_denied_marker(readonly_marker: Path, mocker: MockerFixture) -> None:
    mocker.patch("os.access", autospec=True, return_value=False)
    with pytest.raises(PermissionError):
        SoftFileLock(readonly_marker).acquire(timeout=0)


@pytest.fixture
def readonly_marker(tmp_path: Path) -> Generator[Path]:
    path: Final = tmp_path / "test.lock"
    path.write_text(f"424242\n{socket.gethostname()}-other\n", encoding="utf-8")
    path.chmod(0o444)
    try:
        yield path
    finally:
        with suppress(FileNotFoundError):
            path.chmod(0o644)


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        pytest.param(None, False, id="privileged"),
        pytest.param(PermissionError(EACCES, "denied"), True, id="access-denied"),
        pytest.param(PermissionError(EPERM, "denied"), True, id="operation-denied"),
    ],
)
@pytest.mark.usefixtures("_read_failure")
def test_file_permissions_capability(*, expected: bool) -> None:
    assert runpy.run_module("capabilities")["CAPABILITIES"]["file-permissions"] is expected


@pytest.mark.parametrize(
    "error",
    [
        pytest.param(OSError(EIO, "probe failed"), id="io-error"),
        pytest.param(OSError(ENOENT, "probe failed"), id="missing-file"),
    ],
)
@pytest.mark.usefixtures("_read_failure")
def test_file_permissions_probe_propagates_io_errors(error: OSError) -> None:
    with pytest.raises(OSError, match="probe failed") as raised:
        runpy.run_module("capabilities")
    assert raised.value is error


@pytest.fixture
def _read_failure(mocker: MockerFixture, error: OSError | None) -> None:
    mocker.patch.object(Path, "read_bytes", autospec=True, side_effect=error, return_value=b"")
