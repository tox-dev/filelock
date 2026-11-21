from __future__ import annotations

import os
from typing import TYPE_CHECKING, Final

import pytest

from filelock import SoftFileLock
from filelock._identity import process_start_token
from filelock._soft._records import (
    _MAX_LOCK_FILE_SIZE,
)

if TYPE_CHECKING:
    from pathlib import Path

    from pytest_mock import MockerFixture

from tests.soft.exclusive.recovery_helpers import HOST, REQUIRES_START_TOKEN, holder_record


def test_pid_without_a_lock_file(lock_path: Path) -> None:
    assert SoftFileLock(lock_path).pid is None


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        pytest.param(b"not-a-number\n", None, id="malformed"),
        pytest.param(b"\xff\xfe\n", None, id="non_utf8"),
        pytest.param(b"x" * (_MAX_LOCK_FILE_SIZE + 1), None, id="oversized"),
        pytest.param(b"42\n", None, id="single_line"),
        pytest.param(f"{os.getpid()}\n{HOST}".encode(), None, id="truncated"),
        pytest.param(holder_record(0).encode(), None, id="out_of_range_pid"),
        pytest.param(holder_record(os.getpid()).encode(), os.getpid(), id="valid"),
    ],
)
def test_pid(lock_path: Path, content: bytes, expected: int | None) -> None:
    lock_path.write_bytes(content)
    assert SoftFileLock(lock_path).pid == expected


def test_pid_while_locked(lock_path: Path) -> None:
    lock = SoftFileLock(lock_path)
    with lock:
        assert lock.pid == os.getpid()


def test_is_lock_held_by_us_without_a_lock_file(lock_path: Path) -> None:
    assert SoftFileLock(lock_path).is_lock_held_by_us is False


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        pytest.param(holder_record(os.getpid() + 1), False, id="different_pid"),
        pytest.param(holder_record(os.getpid()), True, id="same_pid"),
        pytest.param(holder_record(os.getpid(), host="other-host"), False, id="same_pid_different_host"),
    ],
)
def test_is_lock_held_by_us(lock_path: Path, content: str, expected: bool) -> None:
    lock_path.write_text(content, encoding="utf-8")
    assert SoftFileLock(lock_path).is_lock_held_by_us is expected


@REQUIRES_START_TOKEN
@pytest.mark.parametrize(
    ("offset", "expected"),
    [pytest.param(0, True, id="matching_start_token"), pytest.param(1, False, id="recycled_pid")],
)
def test_is_lock_held_by_us_checks_start_token(lock_path: Path, offset: int, *, expected: bool) -> None:
    # Acquisition breaks a marker with our PID but another start token as stale, so it must not read as ours either.
    token: Final[int | None] = process_start_token(os.getpid())
    assert token is not None
    lock_path.write_text(holder_record(os.getpid(), start=token + offset), encoding="utf-8")
    assert SoftFileLock(lock_path).is_lock_held_by_us is expected


def test_is_lock_held_by_us_fails_closed_without_own_start_token(lock_path: Path, mocker: MockerFixture) -> None:
    lock_path.write_text(holder_record(os.getpid(), start=1), encoding="utf-8")
    mocker.patch("filelock._identity._process.process_start_token", return_value=None, autospec=True)
    assert SoftFileLock(lock_path).is_lock_held_by_us is False


def test_break_lock(lock_path: Path) -> None:
    lock_path.write_text(holder_record(os.getpid()), encoding="utf-8")
    SoftFileLock(lock_path).break_lock()
    assert not lock_path.exists()


def test_break_lock_without_a_lock_file(lock_path: Path) -> None:
    SoftFileLock(lock_path).break_lock()
    assert not lock_path.exists()
