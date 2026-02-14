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
def test_default_mode_property_returns_0o644(lock_type: type[BaseFileLock], tmp_path: Path) -> None:
    lock = lock_type(tmp_path / "a.lock")
    assert lock.mode == 0o644


@pytest.mark.parametrize("lock_type", [FileLock, SoftFileLock])
def test_explicit_mode_property_returns_value(lock_type: type[BaseFileLock], tmp_path: Path) -> None:
    lock = lock_type(tmp_path / "a.lock", mode=0o600)
    assert lock.mode == 0o600


@pytest.mark.parametrize("lock_type", [FileLock, SoftFileLock])
def test_has_explicit_mode_false_by_default(lock_type: type[BaseFileLock], tmp_path: Path) -> None:
    lock = lock_type(tmp_path / "a.lock")
    assert lock.has_explicit_mode is False


@pytest.mark.parametrize("lock_type", [FileLock, SoftFileLock])
def test_has_explicit_mode_true_when_set(lock_type: type[BaseFileLock], tmp_path: Path) -> None:
    lock = lock_type(tmp_path / "a.lock", mode=0o644)
    assert lock.has_explicit_mode is True


@pytest.mark.parametrize("lock_type", [FileLock, SoftFileLock])
def test_open_mode_returns_0o666_when_unset(lock_type: type[BaseFileLock], tmp_path: Path) -> None:
    lock = lock_type(tmp_path / "a.lock")
    assert lock._open_mode() == 0o666


@pytest.mark.parametrize("lock_type", [FileLock, SoftFileLock])
def test_open_mode_returns_explicit_value(lock_type: type[BaseFileLock], tmp_path: Path) -> None:
    lock = lock_type(tmp_path / "a.lock", mode=0o600)
    assert lock._open_mode() == 0o600


@pytest.mark.skipif(sys.platform == "win32", reason="Windows does not apply umask to file permissions")
@pytest.mark.parametrize("lock_type", [FileLock, SoftFileLock])
def test_default_mode_respects_umask(lock_type: type[BaseFileLock], tmp_path: Path) -> None:
    lock_path = tmp_path / "a.lock"
    lock = lock_type(str(lock_path))

    initial_umask = os.umask(0o022)
    try:
        lock.acquire()
        assert lock.is_locked

        mode = filemode(lock_path.stat().st_mode)
        assert mode == "-rw-r--r--"
    finally:
        os.umask(initial_umask)

    lock.release()


@pytest.mark.skipif(sys.platform == "win32", reason="fchmod only on Unix")
def test_default_mode_skips_fchmod(tmp_path: Path, mocker: MagicMock) -> None:
    lock_path = tmp_path / "a.lock"
    lock = FileLock(str(lock_path))

    fchmod_spy = mocker.patch("os.fchmod")
    lock.acquire()
    assert lock.is_locked
    fchmod_spy.assert_not_called()
    lock.release()


@pytest.mark.skipif(sys.platform == "win32", reason="fchmod only on Unix")
def test_explicit_mode_calls_fchmod(tmp_path: Path, mocker: MagicMock) -> None:
    lock_path = tmp_path / "a.lock"
    lock = FileLock(str(lock_path), mode=0o644)

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

        mode = filemode(lock_path.stat().st_mode)
        assert mode == "-rw-rw-rw-"
    finally:
        os.umask(initial_umask)

    lock.release()


def test_singleton_default_mode_matches(tmp_path: Path) -> None:
    lock_path = tmp_path / "a.lock"
    lock_1 = FileLock(str(lock_path), is_singleton=True)
    lock_2 = FileLock(str(lock_path), is_singleton=True)
    assert lock_1 is lock_2


def test_singleton_explicit_mode_matches(tmp_path: Path) -> None:
    lock_path = tmp_path / "a.lock"
    lock_1 = FileLock(str(lock_path), mode=0o600, is_singleton=True)
    lock_2 = FileLock(str(lock_path), mode=0o600, is_singleton=True)
    assert lock_1 is lock_2


def test_singleton_default_vs_explicit_mode_differ(tmp_path: Path) -> None:
    lock_path = tmp_path / "a.lock"
    lock = FileLock(str(lock_path), is_singleton=True)
    with pytest.raises(ValueError, match="mode"):
        FileLock(str(lock_path), mode=0o644, is_singleton=True)
    del lock


def test_unset_file_mode_sentinel_value() -> None:
    assert _UNSET_FILE_MODE == -1
