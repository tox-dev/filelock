from __future__ import annotations

import os
import stat
from errno import EIO, ENOSPC
from pathlib import Path
from typing import TYPE_CHECKING, Final

import pytest
from capabilities import CAPABILITIES

from filelock import StrictSoftFileLock, Timeout
from filelock._soft._strict._publication import _link_relative, _probe_link_follow_symlinks, open_relative
from filelock._soft._strict._sentinel import _open_sentinel
from tests.capability_marks import (
    NEEDS_SYMLINK,
    NEEDS_UNLINK_OPEN_FILE,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from pytest_mock import MockerFixture
from .capability_marks import CLAIM_LINK_ONLY, NEEDS_DIR_FD, NEEDS_LINK_DIR_FD
from .claim_records import initialize_protocol, write_foreign_claim
from .filesystem_faults import PathValue, fail_directory_close_after

pytestmark: Final[pytest.MarkDecorator] = pytest.mark.requires_hard_links


@pytest.mark.parametrize(
    "initialize",
    [
        pytest.param(lambda _lock_path: None, id="sentinel"),
        pytest.param(initialize_protocol, id="intent", marks=CLAIM_LINK_ONLY),
    ],
)
def test_strict_soft_private_close_failure_preserves_publication(
    tmp_path: Path, mocker: MockerFixture, initialize: Callable[[Path], None]
) -> None:
    lock_path = tmp_path / "resource.lock"
    initialize(lock_path)
    real_close = os.close
    real_open_relative = open_relative
    private_fd: int | None = None

    def capture_private_record(directory_ref: tuple[str, int | None], name: str, flags: int, mode: int) -> int:
        nonlocal private_fd
        fd = real_open_relative(directory_ref, name, flags, mode)
        if flags & os.O_CREAT:  # pragma: no branch  # only the private creation is relevant to this fault
            private_fd = fd
        return fd

    # A background thread may close an unrelated descriptor first.
    def fail_private_close(fd: int) -> None:
        nonlocal private_fd
        real_close(fd)
        if fd == private_fd:  # pragma: no branch  # only the captured descriptor closes during publication
            private_fd = None  # the cleanup that follows opens a directory under the same number
            raise OSError(EIO, "private close failed")

    mocker.patch("filelock._soft._strict._publication.open_relative", side_effect=capture_private_record, autospec=True)
    mocker.patch("os.close", side_effect=fail_private_close, autospec=True)
    lock = StrictSoftFileLock(lock_path)

    with pytest.raises(OSError, match="private close failed"):
        lock.acquire()
    assert (lock.claims, lock.is_locked, lock_path.exists()) == ((), False, True)


@NEEDS_DIR_FD  # pragma: needs dir-fd
def test_strict_soft_link_and_directory_close_failures_preserve_both(tmp_path: Path, mocker: MockerFixture) -> None:
    lock_path = tmp_path / "resource.lock"
    initialize_protocol(lock_path)
    real_close = os.close
    real_fstat = os.fstat
    link_failed = False
    directory_close_failed = False

    def fail_link(_source: PathValue, _destination: PathValue, **_options: int | bool | None) -> None:
        nonlocal link_failed
        link_failed = True
        raise OSError(EIO, "link failed")

    def fail_directory_close(fd: int) -> None:
        nonlocal directory_close_failed
        is_directory = stat.S_ISDIR(real_fstat(fd).st_mode)
        real_close(fd)
        if is_directory and link_failed and not directory_close_failed:
            directory_close_failed = True
            raise OSError(EIO, "directory close failed")

    mocker.patch("os.link", side_effect=fail_link, autospec=True)
    mocker.patch("os.close", side_effect=fail_directory_close, autospec=True)
    lock = StrictSoftFileLock(lock_path)

    with pytest.raises(BaseExceptionGroup) as raised:
        lock.acquire()
    assert ([str(error) for error in raised.value.exceptions], lock.claims, lock.is_locked) == (
        ["[Errno 5] link failed", "[Errno 5] directory close failed"],
        (),
        False,
    )


@NEEDS_DIR_FD  # pragma: needs dir-fd
@NEEDS_LINK_DIR_FD  # pragma: needs link-dir-fd
def test_strict_soft_held_directory_close_failure_removes_both_claims(tmp_path: Path, mocker: MockerFixture) -> None:
    lock_path = tmp_path / "resource.lock"
    initialize_protocol(lock_path)
    fail_directory_close_after(mocker, "link", "held-", "held directory close failed")
    lock = StrictSoftFileLock(lock_path)

    with pytest.raises(OSError, match="held directory close failed"):
        lock.acquire()
    assert (lock.claims, lock.is_locked) == ((), False)


@NEEDS_SYMLINK  # pragma: needs symlink
@NEEDS_UNLINK_OPEN_FILE  # pragma: needs unlink-open-file
@pytest.mark.skipif(  # pragma: needs link-follow-symlinks
    not CAPABILITIES["link-follow-symlinks"], reason="requires no-follow hard links"
)
def test_strict_soft_private_record_swap_does_not_link_target(tmp_path: Path, mocker: MockerFixture) -> None:
    lock_path = tmp_path / "resource.lock"
    (target := tmp_path / "private" / "target").parent.mkdir(mode=0o700)
    target.write_bytes(b"private")
    probe = mocker.patch(
        "tempfile.TemporaryDirectory",
        side_effect=OSError(ENOSPC, "no space left"),
        autospec=True,
    )
    supports_no_follow = _probe_link_follow_symlinks()
    mocker.stop(probe)
    mocker.patch("filelock._soft._strict._publication._LINK_HONORS_FOLLOW_SYMLINKS", supports_no_follow)
    real_link = _link_relative

    def swap_then_link(directory_ref: tuple[str, int | None], source_name: str, destination_name: str) -> None:
        (source := Path(directory_ref[0], source_name)).unlink()
        source.symlink_to(target)
        real_link(directory_ref, source_name, destination_name)

    mocker.patch("filelock._soft._strict._publication._link_relative", autospec=True, side_effect=swap_then_link)

    with pytest.raises(Timeout):
        StrictSoftFileLock(lock_path, timeout=0).acquire()
    assert (target.stat().st_nlink, lock_path.is_symlink()) == (1, True)


def test_strict_soft_sentinel_inspection_and_close_failure_group(tmp_path: Path, mocker: MockerFixture) -> None:
    lock_path = tmp_path / "resource.lock"
    initialize_protocol(lock_path)
    real_fstat = os.fstat
    real_close = os.close
    real_open_sentinel = _open_sentinel
    sentinel_fd: int | None = None

    def capture_sentinel(lock_file: str, path: Path, unchanged_for: Callable[[os.stat_result], float]) -> int | None:
        nonlocal sentinel_fd
        sentinel_fd = real_open_sentinel(lock_file, path, unchanged_for)
        return sentinel_fd

    # Capture the descriptor after the sentinel read so its first failed stat belongs to acquisition.
    def fail_sentinel_inspection(fd: int) -> os.stat_result:
        if fd == sentinel_fd:
            raise OSError(EIO, "sentinel inspection failed")
        return real_fstat(fd)

    def fail_sentinel_close(fd: int) -> None:
        if fd == sentinel_fd:
            raise OSError(EIO, "sentinel close failed")
        # Besides the sentinel this flow closes only the descriptor the lock creates the claim directory against.
        real_close(fd)  # pragma: needs dir-fd

    mocker.patch("filelock._soft._strict._sentinel._open_sentinel", side_effect=capture_sentinel, autospec=True)
    mocker.patch("os.fstat", side_effect=fail_sentinel_inspection, autospec=True)
    mocker.patch("os.close", side_effect=fail_sentinel_close, autospec=True)
    lock = StrictSoftFileLock(lock_path)

    with pytest.raises(BaseExceptionGroup) as raised:
        lock.acquire()
    assert ([str(error) for error in raised.value.exceptions], lock.is_locked) == (
        ["[Errno 5] sentinel inspection failed", "[Errno 5] sentinel close failed"],
        False,
    )


@NEEDS_DIR_FD  # pragma: needs dir-fd
@pytest.mark.parametrize(
    ("initialize", "published"),
    [
        pytest.param(lambda _lock_path: None, "resource.lock", id="sentinel"),
        pytest.param(initialize_protocol, "intent-", id="intent"),
    ],
)
def test_strict_soft_publication_directory_close_failure(
    tmp_path: Path, mocker: MockerFixture, initialize: Callable[[Path], None], published: str
) -> None:
    lock_path = tmp_path / "resource.lock"
    initialize(lock_path)
    fail_directory_close_after(mocker, "link", published, "publication directory close failed")
    lock = StrictSoftFileLock(lock_path, timeout=0)

    with pytest.raises(OSError, match="publication directory close failed"):
        lock.acquire()
    assert lock.is_locked is False


@NEEDS_DIR_FD  # pragma: needs dir-fd
def test_strict_soft_force_break_directory_close_failure(tmp_path: Path, mocker: MockerFixture) -> None:
    lock = StrictSoftFileLock(tmp_path / "resource.lock")
    lock.acquire()
    claim_name = lock.claims[0].name
    fail_directory_close_after(mocker, "unlink", claim_name, "force break directory close failed")

    with pytest.raises(OSError, match="force break directory close failed"):
        lock.force_break(claim_name)
    lock.release()
    assert (lock.is_locked, lock.claims) == (False, ())


def test_strict_soft_release_sentinel_close_failure(tmp_path: Path, mocker: MockerFixture) -> None:
    lock = StrictSoftFileLock(tmp_path / "resource.lock")
    lock.acquire()
    sentinel_fd = lock._context.lock_file_fd
    real_close = os.close

    def fail_sentinel_close(fd: int) -> None:
        if fd == sentinel_fd:
            raise OSError(EIO, "sentinel close failed")
        # Only posix release closes a second, non-sentinel descriptor (the dir_fd); win32 closes only the sentinel.
        real_close(fd)  # pragma: needs dir-fd

    mocker.patch("os.close", side_effect=fail_sentinel_close, autospec=True)

    with pytest.raises(OSError, match="sentinel close failed"):
        lock.release()
    assert (lock.is_locked, lock.claims) == (False, ())


def test_strict_soft_discard_sentinel_close_failure(tmp_path: Path, mocker: MockerFixture) -> None:
    lock_path = tmp_path / "resource.lock"
    initialize_protocol(lock_path)
    write_foreign_claim(Path(f"{lock_path}.filelock") / "claims", "a" * 32)
    real_close = os.close
    real_fstat = os.fstat
    sentinel = lock_path.stat()

    def fail_sentinel_close(fd: int) -> None:
        if os.path.samestat(real_fstat(fd), sentinel):
            raise OSError(EIO, "doorway sentinel close failed")
        real_close(fd)

    mocker.patch("os.close", side_effect=fail_sentinel_close, autospec=True)
    lock = StrictSoftFileLock(lock_path)

    with pytest.raises(OSError, match="doorway sentinel close failed"):
        lock.acquire()
    assert (lock.is_locked, [claim.state for claim in lock.claims]) == (False, ["held"])
