from __future__ import annotations

import os
import subprocess  # ruff:ignore[suspicious-subprocess-import]  # only fixed-argument, no-shell runs below
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from filelock import (
    FileLock,
    Timeout,
)
from tests.capability_marks import (
    NEEDS_SYMLINK,
    WINDOWS_ONLY,
)

if TYPE_CHECKING:
    from pytest_mock import MockerFixture


@WINDOWS_ONLY  # pragma: win32 cover  # pragma: needs symlink
@NEEDS_SYMLINK
def test_windows_reparse_point_lock_file_rejected(tmp_path: Path) -> None:
    target = tmp_path / "target.txt"
    target.write_text("sensitive", encoding="utf-8")
    link = tmp_path / "resource.lock"
    link.symlink_to(target)

    with pytest.raises(OSError, match="reparse point"):
        FileLock(link).acquire()

    assert target.read_text(encoding="utf-8") == "sensitive"


@WINDOWS_ONLY  # pragma: win32 cover
def test_windows_delete_pending_is_treated_as_contention(tmp_path: Path, mocker: MockerFixture) -> None:
    mocker.patch(
        "filelock.internal.filesystem.windows._handles._nt_open", return_value=(0, 0xC0000056), autospec=True
    )  # STATUS_DELETE_PENDING
    with pytest.raises(Timeout):
        FileLock(str(tmp_path / "a"), timeout=0.2).acquire()


@WINDOWS_ONLY  # pragma: win32 cover
def test_windows_permanent_denial_raises_without_timeout(tmp_path: Path, mocker: MockerFixture) -> None:
    mocker.patch("filelock.internal.filesystem.windows._handles._ACCESS_DENIED_PROBES", 10)
    nt_open = mocker.patch(
        "filelock.internal.filesystem.windows._handles._nt_open", return_value=(0, 0xC0000022), autospec=True
    )  # STATUS_ACCESS_DENIED
    with pytest.raises(PermissionError):
        FileLock(str(tmp_path / "a"), timeout=5).acquire()
    assert nt_open.call_count == 11


@WINDOWS_ONLY  # pragma: win32 cover
def test_windows_transient_denial_is_retried_within_grace(tmp_path: Path, mocker: MockerFixture) -> None:
    # A zero timeout allows only one acquire attempt, so the open must absorb the transient access denial.
    assert sys.platform == "win32"
    from filelock.internal.filesystem.windows._handles import _nt_open as real_nt_open

    statuses = [0xC0000022]

    def deny_once(path: str, *, read_only: bool, owner_only: bool) -> tuple[int, int]:
        if statuses:
            return 0, statuses.pop()
        return real_nt_open(path, read_only=read_only, owner_only=owner_only)

    nt_open = mocker.patch(
        "filelock.internal.filesystem.windows._handles._nt_open", side_effect=deny_once, autospec=True
    )
    lock = FileLock(str(tmp_path / "a"), timeout=0)
    lock.acquire()
    assert (lock.is_locked, nt_open.call_count) == (True, 2)
    lock.release()


@WINDOWS_ONLY  # pragma: win32 cover
def test_windows_delete_in_progress_is_contention_not_denial(tmp_path: Path) -> None:
    assert sys.platform == "win32"
    import ctypes

    target = str(tmp_path / "dp.lock")
    kernel32 = ctypes.windll.kernel32
    delete_access, generic_read, share_all, create_always, delete_on_close = (
        0x10000,
        0x80000000,
        0x1 | 0x2 | 0x4,
        2,
        0x04000000,
    )
    # The live handle keeps the file delete-pending until the contender runs.
    handle = kernel32.CreateFileW(
        target, delete_access | generic_read, share_all, None, create_always, delete_on_close, None
    )
    assert handle not in {0, ctypes.c_void_p(-1).value}
    try:
        with pytest.raises(Timeout):
            FileLock(target, timeout=0.3).acquire()
    finally:
        kernel32.CloseHandle(handle)


@WINDOWS_ONLY  # pragma: win32 cover
@pytest.mark.parametrize(
    ("mode", "inherits"),
    [
        pytest.param(-1, True, id="unset"),
        pytest.param(0o644, True, id="others-read"),
        pytest.param(0o600, False, id="owner-only"),
    ],
)
def test_windows_owner_only_mode_keeps_out_the_directory_acl(
    inheriting_directory: Path, mode: int, *, inherits: bool
) -> None:
    lock_path = inheriting_directory / "a"
    with FileLock(str(lock_path), mode=mode):
        acl = subprocess.run(
            [Path(os.environ["SYSTEMROOT"], "System32", "icacls.exe"), lock_path], capture_output=True, check=True
        ).stdout
    assert (b"(I)" in acl) is inherits  # icacls marks inherited entries with (I) in every display language
