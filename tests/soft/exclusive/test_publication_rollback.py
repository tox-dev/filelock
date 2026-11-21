from __future__ import annotations

import itertools
import os
import sys
from contextlib import ExitStack, suppress
from errno import EBADF, EIO, ENODEV, ENOSPC
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Final

import pytest

from filelock import MarkerSoftFileLock, SoftFileLease, SoftFileLock
from filelock._filesystem import pin_inode
from filelock._soft import read_lock_file
from tests.capability_marks import (
    NEEDS_STALE_RECLAIM,
)
from tests.conftest import STALE_BREAKERS, acquire_after_reclaim

if TYPE_CHECKING:
    from collections.abc import Callable
    from types import FrameType

    from _typeshed import Unused
    from pytest_mock import MockerFixture, MockType

from tests.soft.exclusive.recovery_helpers import HOST, hold_reports_foreign_identity


def test_write_failure_rolls_back_acquire(lock_path: Path, mocker: MockerFixture) -> None:
    mocker.patch(
        "filelock._filesystem._files.os.write",
        side_effect=OSError(ENOSPC, "No space left on device"),
        autospec=True,
    )

    lock = SoftFileLock(lock_path)
    with pytest.raises(OSError, match="No space left on device"):
        lock.acquire()
    assert not lock.is_locked
    assert not lock_path.exists()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX rolls back before closing the descriptor")
@NEEDS_STALE_RECLAIM
@pytest.mark.usefixtures("unchanged_past_grace")
@pytest.mark.parametrize(
    ("cleanup_errno", "fail_unlink", "record"),
    [
        pytest.param(ENOSPC, False, b"\0", id="create-full"),
        pytest.param(ENOSPC, True, b"\0", id="unlink-full"),
        pytest.param(EIO, False, b"", id="io-error"),
    ],
)
def test_rollback_on_full_filesystem(
    lock_path: Path, mocker: MockerFixture, cleanup_errno: int, record: bytes, *, fail_unlink: bool
) -> None:  # pragma: win32 no cover
    open_file = os.open

    def open_with_full_directory(path: str, flags: int, mode: int = 0o777) -> int:
        if not fail_unlink and ".break." in path:
            raise OSError(cleanup_errno, os.strerror(cleanup_errno))
        return open_file(path, flags, mode)

    unlink = Path.unlink

    def unlink_on_full_filesystem(path: Path, *, missing_ok: bool = False) -> None:
        if fail_unlink and path == lock_path:
            raise OSError(ENOSPC, os.strerror(ENOSPC))
        unlink(path, missing_ok=missing_ok)

    unlinks = mocker.patch.object(Path, "unlink", side_effect=unlink_on_full_filesystem, autospec=True)
    opens = mocker.patch("filelock._filesystem._files.os.open", side_effect=open_with_full_directory, autospec=True)
    writes = mocker.patch(
        "filelock._filesystem._files.os.write",
        side_effect=OSError(ENOSPC, "record write failed"),
        autospec=True,
    )
    with pytest.raises(OSError, match="record write failed"):
        SoftFileLock(lock_path).acquire(timeout=0)
    mocker.stop(opens)
    mocker.stop(writes)
    mocker.stop(unlinks)
    assert lock_path.read_bytes() == record
    lock = SoftFileLock(lock_path)
    acquire_after_reclaim(lock)
    assert lock.is_locked == (cleanup_errno == ENOSPC)
    lock.release()


def test_short_writes_still_write_the_whole_record(lock_path: Path, mocker: MockerFixture) -> None:
    real_write = os.write
    mocker.patch(
        "filelock._filesystem._files.os.write",
        side_effect=lambda fd, data: real_write(fd, bytes(data[:1])),
        autospec=True,
    )

    with SoftFileLock(lock_path):
        lines = lock_path.read_text(encoding="utf-8").splitlines()
    assert (lines[0], lines[1]) == (str(os.getpid()), HOST)


def test_zero_write_rolls_back_acquire(lock_path: Path, mocker: MockerFixture) -> None:
    mocker.patch("filelock._filesystem._files.os.write", return_value=0, autospec=True)

    lock = SoftFileLock(lock_path)
    with pytest.raises(OSError, match="0 bytes"):
        lock.acquire()
    assert not lock.is_locked
    assert not lock_path.exists()


@pytest.mark.skipif(sys.platform == "win32", reason="Windows unlinks a marker only after closing it")
def test_failed_acquire_spares_a_successor_on_the_freed_inode_number(  # pragma: win32 no cover
    lock_path: Path, mocker: MockerFixture, reuse_inode: Callable[[Path, os.stat_result], None]
) -> None:
    def evicted_and_succeeded_then_write_fails(fd: int, data: bytes) -> int:
        # A contender evicted the still-empty marker, and the next holder's marker gets its inode number once freed.
        ours: Final[os.stat_result] = os.fstat(fd)
        lock_path.unlink()
        lock_path.write_text("live", encoding="utf-8")
        os.utime(lock_path, ns=(ours.st_atime_ns, ours.st_mtime_ns))
        reuse_inode(lock_path, ours)
        raise OSError(ENOSPC, f"No space left on device for {len(data)} bytes")

    mocker.patch(
        "filelock._filesystem._files.os.write",
        side_effect=evicted_and_succeeded_then_write_fails,
        autospec=True,
    )
    with pytest.raises(OSError, match="No space left on device"):
        SoftFileLock(lock_path).acquire()
    mocker.stopall()

    assert lock_path.read_text(encoding="utf-8") == "live"


@pytest.mark.skipif(sys.platform == "win32", reason="Windows cannot exchange two names")
@NEEDS_STALE_RECLAIM
def test_failed_acquire_spares_a_file_that_replaced_its_placeholder(  # pragma: win32 no cover
    lock_path: Path, mocker: MockerFixture
) -> None:
    mocker.patch("os.fchmod", side_effect=OSError(EIO, "Input/output error"), autospec=True)
    lstat = os.lstat
    replaced: list[bool] = []

    def peer_replaces_the_placeholder(path: str | os.PathLike[str], *, dir_fd: int | None = None) -> os.stat_result:
        found: Final[os.stat_result] = lstat(path, dir_fd=dir_fd)
        if ".break." in os.fspath(path) and not replaced:
            replaced.append(True)
            (other := lock_path.with_name("other")).write_text("other", encoding="utf-8")
            other.replace(lock_path)
        return found

    mocker.patch.object(os, "lstat", new=peer_replaces_the_placeholder)
    with pytest.raises(OSError, match="Input/output error"):
        SoftFileLock(lock_path, mode=0o600).acquire()
    mocker.stopall()

    assert (lock_path.read_text(encoding="utf-8"), list(lock_path.parent.glob("test.lock.break.*"))) == ("other", [])


def test_failed_acquire_cleanup_spares_a_replacement(lock_path: Path, mocker: MockerFixture) -> None:
    mocker.patch(
        "filelock._filesystem._files.os.write",
        side_effect=OSError(ENOSPC, "No space left on device"),
        autospec=True,
    )
    hold_reports_foreign_identity(mocker)

    lock = SoftFileLock(lock_path)
    with pytest.raises(OSError, match="No space left on device"):
        lock.acquire()
    assert lock_path.exists()


def test_publication_without_identity_leaves_the_marker(
    lock_path: Path, mocker: MockerFixture, soft_open_spy: MockType
) -> None:
    # The rollback cannot prove the path still names the file it created, so it leaves the marker to stale handling.
    real_fstat = os.fstat

    def no_identity(fd: int) -> os.stat_result:
        if fd == soft_open_spy.spy_return:
            raise OSError(ENODEV, "no identity")
        return real_fstat(fd)

    mocker.patch("os.fstat", autospec=True, side_effect=no_identity)
    lock: Final[SoftFileLock] = SoftFileLock(lock_path)
    with pytest.raises(OSError, match="no identity"):
        lock.acquire()
    assert (lock.is_locked, lock_path.exists()) == (False, True)


def test_transient_identity_failure_still_cleans_the_marker(lock_path: Path, mocker: MockerFixture) -> None:
    # The descriptor still identifies the marker during rollback.
    real_fstat = os.fstat
    calls: Final[itertools.count[int]] = itertools.count()

    def fail_first(fd: int) -> os.stat_result:
        if next(calls) == 0:
            raise OSError(EIO, "Input/output error")
        return real_fstat(fd)

    mocker.patch("os.fstat", side_effect=fail_first, autospec=True)
    lock: Final[SoftFileLock] = SoftFileLock(lock_path)
    with pytest.raises(OSError, match="Input/output error"):
        lock.acquire()
    assert (lock.is_locked, lock_path.exists()) == (False, False)


@pytest.mark.parametrize("lock_type", [SoftFileLock, MarkerSoftFileLock, SoftFileLease])
def test_acquire_interrupted_as_create_returns_removes_marker(lock_path: Path, lock_type: type[SoftFileLock]) -> None:
    lock: Final[SoftFileLock] = lock_type(lock_path)
    with pytest.raises(KeyboardInterrupt):
        _interrupted_once(lock_path.exists, lock.acquire)
    assert (lock.is_locked, lock_path.exists()) == (False, False)
    with lock_type(lock_path, blocking=False) as peer:
        assert peer.is_locked


def _interrupted_once(created: Callable[[], bool], operation: Callable[[], object]) -> None:
    pending: Final[list[type[KeyboardInterrupt]]] = [KeyboardInterrupt]

    def interrupt(_frame: FrameType, event: str, _arg: Unused) -> None:  # pragma: no cover  # profiler runs untraced
        if event == "c_return" and pending and created():
            raise pending.pop()

    sys.setprofile(interrupt)
    try:
        operation()
    finally:
        sys.setprofile(None)


@pytest.mark.skipif(sys.platform == "win32", reason="only POSIX opens an inode pin")
@pytest.mark.parametrize(
    "inspect",
    [
        pytest.param(lambda path, _cleanup: read_lock_file(path), id="read"),
        pytest.param(lambda path, cleanup: cleanup.enter_context(pin_inode(path)), id="pin"),
    ],
)
def test_interrupted_marker_inspection_closes_its_descriptor(
    lock_path: Path, inspect: Callable[[str, ExitStack], object]
) -> None:  # pragma: win32 no cover
    lock_path.touch()
    fd = os.open(lock_path, os.O_RDONLY)
    os.close(fd)

    def inspecting() -> bool:  # pragma: no cover  # the profiler calls this with tracing disabled
        with suppress(OSError):
            return os.path.samestat(os.fstat(fd), lock_path.stat())
        return False

    with ExitStack() as cleanup, pytest.raises(KeyboardInterrupt):
        _interrupted_once(inspecting, partial(inspect, str(lock_path), cleanup))
    with pytest.raises(OSError, check=lambda error: error.errno == EBADF):
        os.fstat(fd)


@pytest.mark.skipif(sys.platform == "win32", reason="Windows cannot exchange two names")
@STALE_BREAKERS
@NEEDS_STALE_RECLAIM
def test_stale_break_interrupted_as_create_returns_removes_marker(  # pragma: win32 no cover
    tmp_path: Path, lock_type: type[SoftFileLock], record: str
) -> None:
    (lock := tmp_path / "test.lock").write_text(record, encoding="utf-8")
    breaker: Final[SoftFileLock] = lock_type(lock)
    with pytest.raises(KeyboardInterrupt):
        _interrupted_once(lambda: any(tmp_path.glob("test.lock.break.*")), partial(breaker.acquire, blocking=False))
    assert (breaker.is_locked, lock.read_text(encoding="utf-8"), sorted(tmp_path.iterdir())) == (False, record, [lock])
