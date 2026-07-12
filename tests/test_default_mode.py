from __future__ import annotations

import os
import sys
from stat import filemode
from typing import TYPE_CHECKING

import pytest

from filelock import FileLock, SoftFileLock
from filelock._api import _UNSET_FILE_MODE, BaseFileLock

if TYPE_CHECKING:
    from pathlib import Path
    from unittest.mock import MagicMock


@pytest.mark.parametrize("lock_type", [FileLock, SoftFileLock])
@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        pytest.param(_UNSET_FILE_MODE, 0o644, id="default"),
        pytest.param(0o600, 0o600, id="explicit"),
    ],
)
def test_mode_property(lock_type: type[BaseFileLock], mode: int, expected: int, tmp_path: Path) -> None:
    assert lock_type(tmp_path / "a.lock", mode=mode).mode == expected


@pytest.mark.parametrize("lock_type", [FileLock, SoftFileLock])
@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        pytest.param(_UNSET_FILE_MODE, False, id="default"),
        pytest.param(0o644, True, id="explicit"),
    ],
)
def test_has_explicit_mode(lock_type: type[BaseFileLock], mode: int, expected: bool, tmp_path: Path) -> None:
    assert lock_type(tmp_path / "a.lock", mode=mode).has_explicit_mode is expected


@pytest.mark.parametrize("lock_type", [FileLock, SoftFileLock])
@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        pytest.param(_UNSET_FILE_MODE, 0o666, id="default"),
        pytest.param(0o600, 0o600, id="explicit"),
    ],
)
def test_open_mode(lock_type: type[BaseFileLock], mode: int, expected: int, tmp_path: Path) -> None:
    assert lock_type(tmp_path / "a.lock", mode=mode)._open_mode() == expected


@pytest.mark.skipif(sys.platform == "win32", reason="Windows does not apply umask to file permissions")
@pytest.mark.parametrize("lock_type", [FileLock, SoftFileLock])
def test_default_mode_respects_umask(lock_type: type[BaseFileLock], tmp_path: Path) -> None:
    lock_path = tmp_path / "a.lock"
    lock = lock_type(str(lock_path))

    initial_umask = os.umask(0o022)
    try:
        lock.acquire()
        assert lock.is_locked
        assert filemode(lock_path.stat().st_mode) == "-rw-r--r--"
    finally:
        os.umask(initial_umask)

    lock.release()


@pytest.mark.skipif(sys.platform == "win32", reason="fchmod only on Unix")
def test_default_mode_skips_fchmod(tmp_path: Path, mocker: MagicMock) -> None:
    lock = FileLock(str(tmp_path / "a.lock"))

    fchmod_spy = mocker.patch("os.fchmod")
    lock.acquire()
    assert lock.is_locked
    fchmod_spy.assert_not_called()
    lock.release()


@pytest.mark.skipif(sys.platform == "win32", reason="fchmod only on Unix")
def test_explicit_mode_calls_fchmod(tmp_path: Path, mocker: MagicMock) -> None:
    lock = FileLock(str(tmp_path / "a.lock"), mode=0o644)

    fchmod_spy = mocker.spy(os, "fchmod")
    lock.acquire()
    assert lock.is_locked
    fchmod_spy.assert_called_once()
    assert fchmod_spy.call_args[0][1] == 0o644
    lock.release()


@pytest.mark.skipif(sys.platform == "win32", reason="fchmod only on Unix")
def test_explicit_mode_overrides_umask(tmp_path: Path) -> None:
    lock_path = tmp_path / "a.lock"
    lock = FileLock(str(lock_path), mode=0o666)

    initial_umask = os.umask(0o022)
    try:
        lock.acquire()
        assert lock.is_locked
        assert filemode(lock_path.stat().st_mode) == "-rw-rw-rw-"
    finally:
        os.umask(initial_umask)

    lock.release()


@pytest.mark.parametrize(
    "mode",
    [
        pytest.param(_UNSET_FILE_MODE, id="default"),
        pytest.param(0o600, id="explicit"),
    ],
)
def test_singleton_same_mode_matches(mode: int, tmp_path: Path) -> None:
    lock_path = str(tmp_path / "a.lock")
    assert FileLock(lock_path, mode=mode, is_singleton=True) is FileLock(lock_path, mode=mode, is_singleton=True)


def test_singleton_default_vs_explicit_mode_differ(tmp_path: Path) -> None:
    lock_path = str(tmp_path / "a.lock")
    lock = FileLock(lock_path, is_singleton=True)
    with pytest.raises(ValueError, match="mode"):
        FileLock(lock_path, mode=0o644, is_singleton=True)
    del lock


def test_unset_file_mode_sentinel_value() -> None:
    assert _UNSET_FILE_MODE == -1
