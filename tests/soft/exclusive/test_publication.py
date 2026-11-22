from __future__ import annotations

import itertools
import os
import subprocess  # ruff:ignore[suspicious-subprocess-import]  # fixed-argument, no-shell audit child
import sys
import textwrap
from typing import TYPE_CHECKING

import pytest

from filelock import SoftFileLock, Timeout
from filelock._identity import process_start_token
from tests.capability_marks import (
    NEEDS_SYMLINK,
)
from tests.conftest import held_after_reclaim

if TYPE_CHECKING:
    from pathlib import Path

    from pytest_mock import MockerFixture

from tests.soft.exclusive.recovery_helpers import HOST, WINDOWS_ONLY


@pytest.mark.parametrize(
    "expected_lines",
    [
        pytest.param(
            [str(os.getpid()), HOST, str(process_start_token(os.getpid()))],
            id="start_token",
            marks=pytest.mark.skipif(
                process_start_token(os.getpid()) is None,
                reason="the start token is unreadable here, e.g. under a time namespace with a boottime offset",
            ),
        ),
        pytest.param(
            [str(os.getpid()), HOST],
            id="no_start_token",
            marks=pytest.mark.skipif(
                process_start_token(os.getpid()) is not None,
                reason="the start token is readable here, so the lock writes it as a third line",
            ),
        ),
    ],
)
def test_lock_writes_pid_and_hostname(lock_path: Path, expected_lines: list[str]) -> None:
    with SoftFileLock(lock_path):
        lines = lock_path.read_text(encoding="utf-8").strip().splitlines()
        assert lines == expected_lines


@NEEDS_SYMLINK  # pragma: needs symlink
def test_dangling_symlink_does_not_create_its_target(lock_path: Path) -> None:
    target = lock_path.with_name("target")
    lock_path.symlink_to(target)
    lock = SoftFileLock(lock_path)

    try:
        with pytest.raises(Timeout):
            lock.acquire(timeout=0)
    finally:
        lock.release()

    assert (lock_path.is_symlink(), target.exists()) == (True, False)


@WINDOWS_ONLY  # pragma: win32 cover
def test_marker_create_reports_a_removed_directory(tmp_path: Path, mocker: MockerFixture) -> None:
    parent = tmp_path / "removed"
    parent.mkdir()
    marker = parent / "job.lock"
    mocker.patch.object(sys, "audit", autospec=True, side_effect=lambda *_: parent.rmdir())

    with pytest.raises(FileNotFoundError) as caught:
        SoftFileLock(marker).acquire(timeout=0)
    assert caught.value.filename == str(marker)


@WINDOWS_ONLY  # pragma: win32 cover
def test_audit_veto_does_not_leave_a_marker(lock_path: Path) -> None:
    code = textwrap.dedent(f"""
        import sys
        from filelock import SoftFileLock

        def veto(event, _args):
            if event == "msvcrt.open_osfhandle":
                raise RuntimeError("audit veto")

        sys.addaudithook(veto)
        try:
            SoftFileLock({str(lock_path)!r}).acquire(timeout=0)
        except RuntimeError as exc:
            print(exc)
    """)
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=False)

    assert (result.returncode, result.stdout.strip(), lock_path.exists()) == (0, "audit veto", False)


@WINDOWS_ONLY  # pragma: win32 cover
@pytest.mark.usefixtures("unchanged_past_grace")
@pytest.mark.parametrize("denied_open", [pytest.param(1, id="pin"), pytest.param(2, id="delete")])
def test_marker_cleanup_denied_closes_its_handles(lock_path: Path, mocker: MockerFixture, denied_open: int) -> None:
    assert sys.platform == "win32"
    import ctypes
    from ctypes import wintypes

    reopen = ctypes.WinDLL("kernel32", use_last_error=True).ReOpenFile
    reopen.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD]
    reopen.restype = wintypes.HANDLE
    attempts = itertools.count(1)

    def deny(handle: int, access: int, share: int, flags: int) -> int:
        if next(attempts) == denied_open:
            return sys.maxsize * 2 + 1
        return reopen(handle, access, share, flags)

    mocker.patch("filelock._filesystem._windows._oplocks.KERNEL32.ReOpenFile", autospec=True, side_effect=deny)
    conversion = mocker.patch("msvcrt.open_osfhandle", autospec=True, side_effect=ValueError("descriptor refused"))
    with pytest.raises(ValueError, match="descriptor refused"):
        SoftFileLock(lock_path).acquire(timeout=0)

    assert lock_path.read_bytes() == b"\n"
    mocker.stop(conversion)
    with held_after_reclaim(SoftFileLock(lock_path)) as recovered:
        assert recovered.is_locked
