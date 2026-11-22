from __future__ import annotations

import os
import runpy
import sys
from contextlib import suppress
from errno import EACCES, EIO, ENOENT, EPERM
from pathlib import Path
from typing import TYPE_CHECKING, Final

import pytest
from capabilities import CAPABILITIES

from filelock import SoftFileLock, Timeout
from filelock._identity import host_name
from tests.capability_marks import NEEDS_FILE_PERMISSIONS

if TYPE_CHECKING:
    from collections.abc import Generator

    from pytest_mock import MockerFixture


_SKIP_AS_ROOT: Final[pytest.MarkDecorator] = pytest.mark.skipif(
    sys.platform != "win32" and os.geteuid() == 0,
    reason="root can write a 0o444 file",
)


_NEEDS_ACCESS_NOFOLLOW: Final[pytest.MarkDecorator] = pytest.mark.skipif(
    not CAPABILITIES["access-nofollow"], reason="os.access cannot refuse to follow a symlink here"
)


@pytest.mark.skipif(sys.platform == "win32", reason="symlink-to-dir raises IsADirectoryError only on Unix")
def test_writability_check_does_not_follow_symlink_to_dir(tmp_path: Path) -> None:  # pragma: win32 no cover
    (target := tmp_path / "targetdir").mkdir()
    (link := tmp_path / "my.lock").symlink_to(target)
    with pytest.raises(Timeout):
        SoftFileLock(link).acquire(blocking=False)


@pytest.mark.skipif(sys.platform == "win32", reason="symlink + 0o444 semantics differ on Windows")
@_SKIP_AS_ROOT  # pragma: win32 no cover
def test_writability_check_does_not_follow_symlink_to_readonly(tmp_path: Path) -> None:
    (target := tmp_path / "readonly").write_text("x", encoding="utf-8")
    target.chmod(0o444)
    (link := tmp_path / "my.lock").symlink_to(target)
    with pytest.raises(Timeout):
        SoftFileLock(link).acquire(blocking=False)


@pytest.mark.skipif(sys.platform == "win32", reason="a real directory raises PermissionError on Windows")
@pytest.mark.parametrize(
    "mode",
    [
        pytest.param(0o755, id="writable"),
        pytest.param(0o555, id="readonly", marks=NEEDS_FILE_PERMISSIONS),
    ],
)
@pytest.mark.parametrize("mtime", [pytest.param(0, id="mtime-zero"), pytest.param(2_000_000_000, id="mtime-future")])
def test_writability_check_rejects_a_directory(tmp_path: Path, mode: int, mtime: int) -> None:  # pragma: win32 no cover
    (path := tmp_path / "a_dir").mkdir()
    os.utime(path, (mtime, mtime))
    path.chmod(mode)
    try:
        with pytest.raises(IsADirectoryError):
            SoftFileLock(path).acquire(blocking=False)
    finally:
        path.chmod(0o755)


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

    def access(filename: str, permission: int, *, effective_ids: bool = False, **_: bool) -> bool:
        assert (filename, permission) == (str(readonly_marker), os.W_OK)
        return effective_ids == supported

    supported: Final[bool] = effective_ids
    probe = mocker.patch("os.access", autospec=True, side_effect=access)
    mocker.patch("os.supports_effective_ids", {True: {probe}, False: set()}[effective_ids])
    with pytest.raises(Timeout):
        SoftFileLock(readonly_marker).acquire(timeout=0)


def test_writability_allows_creation_after_marker_disappears(readonly_marker: Path, mocker: MockerFixture) -> None:
    def access(filename: str, permission: int, *, effective_ids: bool = False, **_: bool) -> bool:
        assert (permission, effective_ids) == (os.W_OK, os.access in os.supports_effective_ids)
        readonly_marker.chmod(0o644)
        Path(filename).unlink()
        return False

    mocker.patch("os.access", autospec=True, side_effect=access)
    with (lock := SoftFileLock(readonly_marker)).acquire(timeout=0):
        assert lock.is_locked


@_NEEDS_ACCESS_NOFOLLOW  # pragma: needs access-nofollow
def test_writability_check_ignores_symlink_swapped_in_after_lstat(readonly_marker: Path, mocker: MockerFixture) -> None:
    def swap_then_access(filename: str, permission: int, **kwargs: bool) -> bool:
        Path(filename).unlink()
        Path(filename).symlink_to(readonly_marker.with_name("missing"))
        return real_access(filename, permission, **kwargs)

    real_access = os.access
    probe = mocker.patch("os.access", autospec=True, side_effect=swap_then_access)
    mocker.patch("os.supports_follow_symlinks", {*os.supports_follow_symlinks, probe})
    with pytest.raises(Timeout):
        SoftFileLock(readonly_marker).acquire(timeout=0)


def test_writability_rejects_denied_marker(readonly_marker: Path, mocker: MockerFixture) -> None:
    mocker.patch("os.access", autospec=True, return_value=False)
    with pytest.raises(PermissionError):
        SoftFileLock(readonly_marker).acquire(timeout=0)


@pytest.fixture
def readonly_marker(tmp_path: Path) -> Generator[Path]:
    path: Final[Path] = tmp_path / "test.lock"
    path.write_text(f"424242\n{host_name()}-other\n", encoding="utf-8")
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
