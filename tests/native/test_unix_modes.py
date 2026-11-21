from __future__ import annotations

import os
from errno import EIO, EWOULDBLOCK
from stat import S_IMODE, filemode
from typing import TYPE_CHECKING

import pytest

from filelock import (
    FileLock,
    Timeout,
)
from tests.capability_marks import (
    NEEDS_FCNTL,
)

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from pytest_mock import MockerFixture


def test_lock_mode(tmp_path: Path) -> None:
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


def test_umask(tmp_path: Path) -> None:
    lock = FileLock(str(tmp_path / "a.lock"), mode=0o666)

    initial_umask = os.umask(0)
    os.umask(initial_umask)

    lock.acquire()
    assert lock.is_locked

    current_umask = os.umask(0)
    os.umask(current_umask)
    assert initial_umask == current_umask

    lock.release()


@pytest.fixture
def held_lock_path(tmp_path: Path) -> Iterator[Path]:  # pragma: needs fcntl
    path = tmp_path / "resource.lock"
    with FileLock(path, timeout=0):
        yield path


@NEEDS_FCNTL  # pragma: needs fcntl
@pytest.mark.parametrize("mode", [pytest.param(-1, id="default-mode"), pytest.param(0o666, id="explicit-mode")])
def test_winner_leaves_existing_file_as_found(tmp_path: Path, mode: int) -> None:
    (journal := tmp_path / "journal.log").write_text("data", encoding="utf-8")
    journal.chmod(0o600)
    # A regular file with one name is indistinguishable from a lock file an earlier holder left.
    journal.rename(lock_path := tmp_path / "resource.lock")

    with FileLock(lock_path, timeout=0, mode=mode):
        assert (lock_path.read_text(encoding="utf-8"), S_IMODE(lock_path.stat().st_mode)) == ("data", 0o600)


@NEEDS_FCNTL  # pragma: needs fcntl
def test_creator_that_loses_the_flock_applies_mode(tmp_path: Path, mocker: MockerFixture) -> None:
    lock_path = tmp_path / "resource.lock"
    # A peer may win the flock first, and only the creator may set the mode.
    mocker.patch(
        "filelock._native._unix.fcntl.flock",
        side_effect=OSError(EWOULDBLOCK, "Resource temporarily unavailable"),
        autospec=True,
    )

    initial_umask = os.umask(0o077)
    try:
        with pytest.raises(Timeout):
            FileLock(lock_path, timeout=0, mode=0o666).acquire()
    finally:
        os.umask(initial_umask)

    assert S_IMODE(lock_path.stat().st_mode) == 0o666


@NEEDS_FCNTL  # pragma: needs fcntl
def test_contender_preserves_holder_contents(held_lock_path: Path) -> None:
    held_lock_path.write_text("holder metadata", encoding="utf-8")

    with pytest.raises(Timeout):
        FileLock(held_lock_path, timeout=0).acquire()

    assert held_lock_path.read_text(encoding="utf-8") == "holder metadata"


@NEEDS_FCNTL  # pragma: needs fcntl
def test_contender_preserves_holder_mode(held_lock_path: Path) -> None:
    held_lock_path.chmod(0o600)

    with pytest.raises(Timeout):
        FileLock(held_lock_path, timeout=0, mode=0o644).acquire()

    assert S_IMODE(held_lock_path.stat().st_mode) == 0o600


@NEEDS_FCNTL  # pragma: needs fcntl
def test_contender_does_not_fchmod(held_lock_path: Path, mocker: MockerFixture) -> None:
    fchmod_spy = mocker.spy(os, "fchmod")

    with pytest.raises(Timeout):
        FileLock(held_lock_path, timeout=0, mode=0o644).acquire()

    fchmod_spy.assert_not_called()


@NEEDS_FCNTL  # pragma: needs fcntl
@pytest.mark.parametrize("failing", [pytest.param("fchmod", id="fchmod"), pytest.param("fstat", id="fstat")])
def test_post_open_failure_closes_fd(tmp_path: Path, mocker: MockerFixture, failing: str) -> None:
    open_spy = mocker.spy(os, "open")
    close_spy = mocker.spy(os, "close")
    real = getattr(os, failing)

    def fail_for_the_lock_file(fd: int, *args: int) -> os.stat_result | None:
        if fd == open_spy.spy_return:
            raise OSError(EIO, "Input/output error")
        return real(fd, *args)

    mocker.patch(f"os.{failing}", side_effect=fail_for_the_lock_file, autospec=True)

    with pytest.raises(OSError, match="Input/output error"):
        FileLock(tmp_path / "resource.lock", timeout=0, mode=0o644).acquire()

    assert any(call.args and call.args[0] == open_spy.spy_return for call in close_spy.call_args_list)
