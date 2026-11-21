from __future__ import annotations

import os
import stat
import time
from errno import EACCES, EIO, ENOENT
from pathlib import Path
from typing import TYPE_CHECKING, Final

import pytest

from filelock import SoftFileLockProtocolError, StrictSoftFileLock, Timeout
from filelock._soft._strict._directories import SUPPORTS, relative_identity
from filelock._soft._strict._publication import open_relative

if TYPE_CHECKING:
    from collections.abc import Callable

    from pytest_mock import MockerFixture
from .capability_marks import CLAIM_LINK_ONLY, NEEDS_DIR_FD, NEEDS_LINK_DIR_FD, WINDOWS_ONLY
from .claim_records import initialize_protocol
from .filesystem_faults import PathValue, patch_strict_clock

pytestmark: Final[pytest.MarkDecorator] = pytest.mark.requires_hard_links


@pytest.mark.parametrize(
    "initialize",
    [
        pytest.param(lambda _lock_path: None, id="sentinel"),
        pytest.param(initialize_protocol, id="intent", marks=CLAIM_LINK_ONLY),
    ],
)
def test_strict_soft_publication_record_replaced_aborts(
    tmp_path: Path, mocker: MockerFixture, initialize: Callable[[Path], None]
) -> None:
    lock_path = tmp_path / "resource.lock"
    initialize(lock_path)
    real_identity = relative_identity

    def mismatch_public(directory_ref: tuple[str, int | None], name: str) -> tuple[int, int] | None:
        identity = real_identity(directory_ref, name)
        if identity is not None and not name.startswith("."):
            return identity[0], identity[1] + 1
        return identity

    mocker.patch("filelock._soft._strict._publication.relative_identity", side_effect=mismatch_public, autospec=True)
    lock = StrictSoftFileLock(lock_path, timeout=0)

    with pytest.raises(Timeout):
        lock.acquire()
    assert lock.is_locked is False


@pytest.mark.parametrize(
    "initialize",
    [
        pytest.param(lambda _lock_path: None, id="sentinel"),
        pytest.param(initialize_protocol, id="intent", marks=CLAIM_LINK_ONLY),
    ],
)
def test_strict_soft_publication_record_reclaimed_aborts(
    tmp_path: Path, mocker: MockerFixture, initialize: Callable[[Path], None]
) -> None:
    lock_path = tmp_path / "resource.lock"
    initialize(lock_path)
    real_identity = relative_identity

    def source_gone(directory_ref: tuple[str, int | None], name: str) -> tuple[int, int] | None:
        if name.startswith("."):
            return None
        # Only the dot-prefixed private record is inspected in this flow, so this passthrough never runs.
        return real_identity(directory_ref, name)  # pragma: no cover

    mocker.patch("filelock._soft._strict._publication.relative_identity", side_effect=source_gone, autospec=True)
    mocker.patch(
        "filelock._soft._strict._publication._link_relative",
        side_effect=FileNotFoundError(ENOENT, "source vanished"),
        autospec=True,
    )
    lock = StrictSoftFileLock(lock_path, timeout=0)

    with pytest.raises(Timeout):
        lock.acquire()
    assert lock.is_locked is False


@pytest.mark.parametrize(
    "initialize",
    [
        pytest.param(lambda _lock_path: None, id="sentinel"),
        pytest.param(initialize_protocol, id="intent", marks=CLAIM_LINK_ONLY),
    ],
)
def test_strict_soft_private_inspection_failure_unlinks_vanished_record(
    tmp_path: Path, mocker: MockerFixture, initialize: Callable[[Path], None]
) -> None:
    lock_path = tmp_path / "resource.lock"
    initialize(lock_path)
    real_fstat = os.fstat
    real_open_relative = open_relative
    private_fd: int | None = None

    def capture_private_record(directory_ref: tuple[str, int | None], name: str, flags: int, mode: int) -> int:
        nonlocal private_fd
        fd = real_open_relative(directory_ref, name, flags, mode)
        if flags & os.O_CREAT:  # pragma: no branch  # only the private creation is relevant to this fault
            private_fd = fd
        return fd

    def fail_private_inspection(fd: int) -> os.stat_result:
        if fd == private_fd:
            raise OSError(EIO, "private inspection failed")
        return real_fstat(fd)

    mocker.patch("filelock._soft._strict._publication.open_relative", side_effect=capture_private_record, autospec=True)
    mocker.patch("os.fstat", side_effect=fail_private_inspection, autospec=True)
    mocker.patch(
        "filelock._soft._strict._publication.unlink_relative",
        side_effect=FileNotFoundError(ENOENT, "record gone"),
        autospec=True,
    )
    lock = StrictSoftFileLock(lock_path)

    with pytest.raises(OSError, match="private inspection failed"):
        lock.acquire()
    assert lock.is_locked is False


@pytest.mark.parametrize(
    "initialize",
    [
        pytest.param(lambda _lock_path: None, id="sentinel"),
        pytest.param(initialize_protocol, id="intent", marks=CLAIM_LINK_ONLY),
    ],
)
def test_strict_soft_private_record_reclaimed_after_close_is_tolerated(
    tmp_path: Path, mocker: MockerFixture, initialize: Callable[[Path], None]
) -> None:
    lock_path = tmp_path / "resource.lock"
    initialize(lock_path)
    real_close = os.close
    real_open_relative = open_relative
    private_records: dict[int, Path] = {}
    reclaimed = 0

    def capture_private_record(directory_ref: tuple[str, int | None], name: str, flags: int, mode: int) -> int:
        fd = real_open_relative(directory_ref, name, flags, mode)
        if flags & os.O_CREAT:  # pragma: no branch  # only private records should be reclaimed
            private_records[fd] = Path(directory_ref[0], name)
        return fd

    def reclaim_on_close(fd: int) -> None:
        nonlocal reclaimed
        real_close(fd)
        if (record := private_records.pop(fd, None)) is not None:
            record.unlink()
            reclaimed += 1

    mocker.patch("filelock._soft._strict._publication.open_relative", side_effect=capture_private_record, autospec=True)
    mocker.patch("os.close", side_effect=reclaim_on_close, autospec=True)
    lock = StrictSoftFileLock(lock_path, timeout=0)

    lock.acquire()
    assert lock.is_locked is True
    lock.release()
    assert lock.is_locked is False
    assert reclaimed > 0


@pytest.mark.parametrize(
    "initialize",
    [
        pytest.param(lambda _lock_path: None, id="sentinel"),
        pytest.param(initialize_protocol, id="intent", marks=CLAIM_LINK_ONLY),
    ],
)
def test_strict_soft_record_finalization_close_and_unlink_failures_group(
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

    def fail_private_close(fd: int) -> None:
        nonlocal private_fd
        real_close(fd)
        if fd == private_fd:  # pragma: no branch  # only the captured descriptor closes during publication
            private_fd = None  # the cleanup that follows opens a directory under the same number
            raise OSError(EIO, "private close failed")

    mocker.patch("filelock._soft._strict._publication.open_relative", side_effect=capture_private_record, autospec=True)
    mocker.patch("os.close", side_effect=fail_private_close, autospec=True)
    mocker.patch(
        "filelock._soft._strict._publication.unlink_relative",
        side_effect=PermissionError(EACCES, "unlink denied"),
        autospec=True,
    )
    lock = StrictSoftFileLock(lock_path)

    with pytest.raises(BaseExceptionGroup) as raised:
        lock.acquire()
    assert (sorted(str(error) for error in raised.value.exceptions), lock.is_locked) == (
        ["[Errno 13] unlink denied", "[Errno 5] private close failed"],
        False,
    )


def test_strict_soft_acquires_without_dir_fd(tmp_path: Path, mocker: MockerFixture) -> None:
    lock_path = tmp_path / "resource.lock"
    mocker.patch.object(type(SUPPORTS), "open_dir_fd", False)
    mocker.patch.object(type(SUPPORTS), "unlink_dir_fd", False)
    mocker.patch.object(type(SUPPORTS), "stat_dir_fd", False)
    mocker.patch.object(type(SUPPORTS), "link_dir_fd", False)
    mocker.patch.object(type(SUPPORTS), "mkdir_dir_fd", False)
    mocker.patch.object(type(SUPPORTS), "scandir_fd", False)

    with StrictSoftFileLock(lock_path) as lock:
        assert (lock.is_locked, [claim.state for claim in lock.claims]) == (True, ["held", "intent"])
    assert StrictSoftFileLock(lock_path).claims == ()


@NEEDS_DIR_FD  # pragma: needs dir-fd
@NEEDS_LINK_DIR_FD  # pragma: needs link-dir-fd
def test_strict_soft_held_link_and_directory_close_failure(tmp_path: Path, mocker: MockerFixture) -> None:
    lock_path = tmp_path / "resource.lock"
    initialize_protocol(lock_path)
    real_close = os.close
    real_fstat = os.fstat
    real_link = os.link
    held_failed = False
    directory_closed = False

    def fail_held_link(
        source: PathValue,
        destination: PathValue,
        *,
        src_dir_fd: int | None = None,
        dst_dir_fd: int | None = None,
        follow_symlinks: bool = True,
    ) -> None:
        nonlocal held_failed
        if Path(os.fsdecode(destination)).name.startswith("held-"):
            held_failed = True
            raise OSError(EIO, "held link failed")
        real_link(source, destination, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd, follow_symlinks=follow_symlinks)

    def fail_directory_close_after_held(fd: int) -> None:
        nonlocal directory_closed
        is_directory = stat.S_ISDIR(real_fstat(fd).st_mode)
        real_close(fd)
        if is_directory and held_failed and not directory_closed:
            directory_closed = True
            raise OSError(EIO, "held link directory close failed")

    mocker.patch("os.link", side_effect=fail_held_link, autospec=True)
    mocker.patch("os.close", side_effect=fail_directory_close_after_held, autospec=True)
    lock = StrictSoftFileLock(lock_path)

    with pytest.raises(BaseExceptionGroup) as raised:
        lock.acquire()
    assert ([str(error) for error in raised.value.exceptions], lock.is_locked, lock.claims) == (
        ["[Errno 5] held link failed", "[Errno 5] held link directory close failed"],
        False,
        (),
    )


def test_strict_soft_opened_record_non_regular_fails_closed(tmp_path: Path, mocker: MockerFixture) -> None:
    lock_path = tmp_path / "resource.lock"
    claims = Path(f"{lock_path}.filelock") / "claims"
    claims.mkdir(parents=True)
    (claims / f"held-v1-{'0' * 32}.claim").write_bytes(b"filelock-strict-v1\n" + b"0" * 32 + b"\n1\n686f7374\n4242\n")
    real_fstat = os.fstat

    def non_regular_fstat(fd: int) -> os.stat_result:
        return os.stat_result((stat.S_IFIFO | 0o644, *real_fstat(fd)[1:]))

    mocker.patch("os.fstat", side_effect=non_regular_fstat, autospec=True)

    with pytest.raises(SoftFileLockProtocolError, match="is not a regular file"):
        _ = StrictSoftFileLock(lock_path).claims


@WINDOWS_ONLY  # pragma: win32 cover
def test_strict_soft_force_break_retries_a_transient_unlink_denial(
    initialized_lock_path: Path, mocker: MockerFixture
) -> None:
    lock = StrictSoftFileLock(initialized_lock_path)
    lock.acquire()
    claim = lock.claims[0].name
    real_unlink = os.unlink
    denied: list[str] = []

    def deny_once(path: PathValue, *, dir_fd: int | None = None) -> None:
        if not denied and os.fsdecode(path).endswith(".claim"):
            denied.append(os.fsdecode(path))
            raise PermissionError(EACCES, "sharing violation")
        real_unlink(path, dir_fd=dir_fd)

    mocker.patch("os.unlink", side_effect=deny_once, autospec=True)
    sleep = mocker.create_autospec(time.sleep)
    patch_strict_clock(mocker, "_publication", sleep=sleep)
    lock.force_break(claim)
    lock.release()

    assert (len(denied), sleep.call_args_list, lock.claims) == (1, [mocker.call(0.001)], ())
