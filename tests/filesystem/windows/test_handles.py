from __future__ import annotations

import sys
from typing import TYPE_CHECKING

import pytest

from filelock import FileLock, Timeout
from tests.capability_marks import WINDOWS_ONLY

if TYPE_CHECKING:
    from pathlib import Path

    from pytest_mock import MockerFixture


@WINDOWS_ONLY  # pragma: win32 cover
def test_windows_delete_pending_is_treated_as_contention(tmp_path: Path, mocker: MockerFixture) -> None:
    mocker.patch(
        "filelock._filesystem._windows._handles._nt_open", return_value=(0, 0xC0000056), autospec=True
    )  # STATUS_DELETE_PENDING
    with pytest.raises(Timeout):
        FileLock(str(tmp_path / "a"), timeout=0.2).acquire()


@WINDOWS_ONLY  # pragma: win32 cover
def test_windows_permanent_denial_raises_without_timeout(tmp_path: Path, mocker: MockerFixture) -> None:
    mocker.patch("filelock._filesystem._windows._handles._ACCESS_DENIED_PROBES", 10)
    nt_open = mocker.patch(
        "filelock._filesystem._windows._handles._nt_open", return_value=(0, 0xC0000022), autospec=True
    )  # STATUS_ACCESS_DENIED
    with pytest.raises(PermissionError):
        FileLock(str(tmp_path / "a"), timeout=5).acquire()
    assert nt_open.call_count == 11


@WINDOWS_ONLY  # pragma: win32 cover
def test_windows_transient_denial_is_retried_within_grace(tmp_path: Path, mocker: MockerFixture) -> None:
    # A zero timeout allows only one acquire attempt, so the open must absorb the transient access denial.
    assert sys.platform == "win32"
    from filelock._filesystem._windows._handles import _nt_open as real_nt_open

    statuses = [0xC0000022]

    def deny_once(path: str, *, read_only: bool, owner_only: bool) -> tuple[int, int]:
        if statuses:
            return 0, statuses.pop()
        return real_nt_open(path, read_only=read_only, owner_only=owner_only)

    nt_open = mocker.patch("filelock._filesystem._windows._handles._nt_open", side_effect=deny_once, autospec=True)
    lock = FileLock(str(tmp_path / "a"), timeout=0)
    lock.acquire()
    assert (lock.is_locked, nt_open.call_count) == (True, 2)
    lock.release()
