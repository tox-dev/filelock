from __future__ import annotations

import os
import subprocess  # ruff:ignore[suspicious-subprocess-import]  # only fixed-argument, no-shell runs below
import sys
from pathlib import Path

import pytest

from filelock import (
    FileLock,
    Timeout,
)
from tests.capability_marks import (
    NEEDS_SYMLINK,
    WINDOWS_ONLY,
)


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
