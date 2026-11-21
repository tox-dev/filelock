from __future__ import annotations

import os
import socket
import stat
from errno import EACCES, EIO
from pathlib import Path
from typing import TYPE_CHECKING, Final

import pytest

from filelock import StrictSoftFileLock
from filelock._soft._strict._directories import SUPPORTS

if TYPE_CHECKING:
    from collections.abc import Iterator

    from pytest_mock import MockerFixture
from .capability_marks import NEEDS_DIR_FD
from .claim_records import initialize_protocol
from .filesystem_faults import PathValue, fail_directory_close_after

pytestmark: Final[pytest.MarkDecorator] = pytest.mark.requires_hard_links


@NEEDS_DIR_FD  # pragma: needs dir-fd
def test_strict_soft_release_error_keeps_remaining_claim(tmp_path: Path, mocker: MockerFixture) -> None:
    lock = StrictSoftFileLock(tmp_path / "resource.lock")
    lock.acquire()
    claim_name = lock.claims[0].name
    real_unlink = Path.unlink
    failed = False

    def fail_once(path: Path, *, missing_ok: bool = False) -> None:
        nonlocal failed
        if not failed:
            failed = True
            assert path.name == claim_name
            raise PermissionError(EACCES, "claim is not writable")
        real_unlink(path, missing_ok=missing_ok)

    mocker.patch.object(type(SUPPORTS), "unlink_dir_fd", False)
    mocker.patch.object(Path, "unlink", autospec=True, side_effect=fail_once)
    with pytest.raises(PermissionError, match="claim is not writable"):
        lock.release()
    assert (lock.is_locked, [claim.name for claim in lock.claims]) == (True, [claim_name])

    lock.release()
    assert (lock.is_locked, lock.claims) == (False, ())


@NEEDS_DIR_FD  # pragma: needs dir-fd
def test_strict_soft_release_commits_before_directory_close_error(tmp_path: Path, mocker: MockerFixture) -> None:
    lock_path = tmp_path / "resource.lock"
    lock = StrictSoftFileLock(lock_path)
    lock.acquire()
    fail_directory_close_after(mocker, "unlink", "held-", "directory close failed")

    with pytest.raises(OSError, match="directory close failed"):
        lock.release()
    mocker.stopall()
    assert (lock.is_locked, lock.lock_counter, lock.claims) == (False, 0, ())
    with StrictSoftFileLock(lock_path, timeout=0) as contender:
        assert contender.is_locked


@NEEDS_DIR_FD  # pragma: needs dir-fd
def test_strict_soft_release_preserves_unlink_and_directory_close_errors(tmp_path: Path, mocker: MockerFixture) -> None:
    lock = StrictSoftFileLock(tmp_path / "resource.lock")
    lock.acquire()
    real_close = os.close
    real_fstat = os.fstat
    real_unlink = os.unlink
    unlink_failed = False
    directory_close_failed = False

    def fail_claim_unlink(path: PathValue, *, dir_fd: int | None = None) -> None:
        nonlocal unlink_failed
        if Path(os.fsdecode(path)).name.startswith("held-"):
            unlink_failed = True
            raise PermissionError(EACCES, "claim unlink failed")
        real_unlink(path, dir_fd=dir_fd)

    def fail_directory_close(fd: int) -> None:
        nonlocal directory_close_failed
        is_directory = stat.S_ISDIR(real_fstat(fd).st_mode)
        real_close(fd)
        if is_directory and unlink_failed and not directory_close_failed:
            directory_close_failed = True
            raise OSError(EIO, "directory close failed")

    unlink_mock = mocker.patch("os.unlink", side_effect=fail_claim_unlink, autospec=True)
    supports_dir_fd_mock = mocker.patch.object(os, "supports_dir_fd", os.supports_dir_fd | {unlink_mock})
    close_mock = mocker.patch("os.close", side_effect=fail_directory_close, autospec=True)

    with pytest.raises(BaseExceptionGroup) as raised:
        lock.release()
    mocker.stop(unlink_mock)
    mocker.stop(supports_dir_fd_mock)
    mocker.stop(close_mock)
    assert (
        [str(error) for error in raised.value.exceptions],
        lock.is_locked,
        lock.lock_counter,
        [claim.state for claim in lock.claims],
    ) == (
        ["[Errno 13] claim unlink failed", "[Errno 5] directory close failed"],
        True,
        1,
        ["held"],
    )
    lock.release()


@NEEDS_DIR_FD  # pragma: needs dir-fd
def test_strict_soft_doorway_preserves_every_claim_cleanup_error(tmp_path: Path, mocker: MockerFixture) -> None:
    lock_path = tmp_path / "resource.lock"
    initialize_protocol(lock_path)
    owner_token = "f" * 32
    competitor_token = "0" * 32
    competitor_name = f"held-v1-{competitor_token}.claim"
    real_close = os.close
    real_fstat = os.fstat
    real_scandir = os.scandir
    real_unlink = os.unlink
    cleanup_name: str | None = None
    competitor_inserted = False

    def add_competitor_before_final_scan(path: int | str) -> Iterator[os.DirEntry[str]]:
        nonlocal competitor_inserted
        claim_directory = Path(f"{lock_path}.filelock", "claims")
        if not competitor_inserted and (claim_directory / f"held-v1-{owner_token}.claim").exists():
            competitor_inserted = True
            (claim_directory / competitor_name).write_text(
                f"filelock-strict-v1\n{competitor_token}\n{os.getpid()}\n{socket.gethostname().encode().hex()}\n4242\n",
                encoding="ascii",
                newline="",
            )
            return real_scandir(claim_directory)
        return real_scandir(path)

    def fail_held_unlink(path: PathValue, *, dir_fd: int | None = None) -> None:
        nonlocal cleanup_name
        cleanup_name = Path(os.fsdecode(path)).name
        if cleanup_name == f"held-v1-{owner_token}.claim":
            raise PermissionError(EACCES, "held unlink failed")
        real_unlink(path, dir_fd=dir_fd)

    def fail_cleanup_directory_close(fd: int) -> None:
        nonlocal cleanup_name
        is_directory = stat.S_ISDIR(real_fstat(fd).st_mode)
        real_close(fd)
        if not is_directory:
            return
        # Only the first directory closed after an unlink is the one it ran in; the next walk closes others.
        unlinked, cleanup_name = cleanup_name, None
        if unlinked == f"intent-v1-{owner_token}.claim":
            raise OSError(EIO, "intent directory close failed")
        if unlinked == f"held-v1-{owner_token}.claim":
            raise OSError(EIO, "held directory close failed")

    mocker.patch("secrets.token_hex", return_value=owner_token, autospec=True)
    mocker.patch("os.scandir", side_effect=add_competitor_before_final_scan, autospec=True)
    unlink_mock = mocker.patch("os.unlink", side_effect=fail_held_unlink, autospec=True)
    supports_dir_fd_mock = mocker.patch.object(os, "supports_dir_fd", os.supports_dir_fd | {unlink_mock})
    close_mock = mocker.patch("os.close", side_effect=fail_cleanup_directory_close, autospec=True)
    lock = StrictSoftFileLock(lock_path)

    with pytest.raises(BaseExceptionGroup) as raised:
        lock.acquire()
    mocker.stop(unlink_mock)
    mocker.stop(supports_dir_fd_mock)
    mocker.stop(close_mock)
    assert (
        _leaf_error_messages(raised.value),
        [claim.name for claim in lock.claims],
        lock.is_locked,
    ) == (
        [
            "[Errno 13] held unlink failed",
            "[Errno 5] held directory close failed",
            "[Errno 5] intent directory close failed",
        ],
        [competitor_name, f"held-v1-{owner_token}.claim"],
        False,
    )
    lock.force_break(competitor_name)
    with lock:
        assert len(lock.claims) == 2


def _leaf_error_messages(error: BaseException) -> list[str]:  # pragma: needs dir-fd
    if isinstance(error, BaseExceptionGroup):
        return [message for child in error.exceptions for message in _leaf_error_messages(child)]
    return [str(error)]
