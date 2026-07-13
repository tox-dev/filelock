from __future__ import annotations

import ctypes
import importlib
import sys
from errno import EACCES
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path

    from pytest_mock import MockerFixture


def test_open_permission_error_is_not_reported_as_contention(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mocker: MockerFixture,
) -> None:
    """A permanent descriptor-open denial must escape before the polling loop."""
    import filelock._windows as windows_module

    fake_kernel32 = mocker.Mock()
    fake_kernel32.GetFileAttributesW = mocker.Mock(return_value=0xFFFFFFFF)
    fake_msvcrt = mocker.Mock()
    fake_msvcrt.LK_NBLCK = 1
    fake_msvcrt.LK_UNLCK = 0
    fake_msvcrt.locking = mocker.Mock()

    original_platform = sys.platform
    monkeypatch.setitem(sys.modules, "msvcrt", fake_msvcrt)
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(ctypes, "WinDLL", mocker.Mock(return_value=fake_kernel32), raising=False)

    try:
        windows_module = importlib.reload(windows_module)
        lock_path = tmp_path / "denied.lock"
        denial = PermissionError(EACCES, "access denied", str(lock_path))
        mocker.patch.object(windows_module.os, "open", side_effect=denial)

        lock = windows_module.WindowsFileLock(lock_path, timeout=0)
        with pytest.raises(PermissionError) as raised:
            lock.acquire()

        assert raised.value is denial
        fake_msvcrt.locking.assert_not_called()
    finally:
        monkeypatch.setattr(sys, "platform", original_platform)
        importlib.reload(windows_module)
