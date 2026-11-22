from __future__ import annotations

import os
import sys
import time
from typing import TYPE_CHECKING

import pytest

from filelock import (
    BaseFileLock,
    FileLock,
    Timeout,
)
from tests.capability_marks import (
    NEEDS_SYMLINK,
    WINDOWS_ONLY,
)
from tests.process_helpers import call_within

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from pytest_mock import MockerFixture


@WINDOWS_ONLY  # pragma: win32 cover
@pytest.mark.parametrize(
    ("timeout", "blocking"), [pytest.param(0.1, True, id="timeout"), pytest.param(-1, False, id="non-blocking")]
)
def test_windows_oplock_on_lock_file_does_not_outlast_deadline(
    oplocked_lock_path: Path, timeout: float, *, blocking: bool
) -> None:
    started = time.monotonic()
    with pytest.raises(Timeout):
        FileLock(str(oplocked_lock_path), is_singleton=False).acquire(timeout=timeout, blocking=blocking)
    assert time.monotonic() - started < 5


@WINDOWS_ONLY  # pragma: win32 cover
def test_windows_nonblocking_acquire_does_not_wait_for_oplock(tmp_path: Path, mocker: MockerFixture) -> None:
    nt_open = mocker.patch(
        "filelock._filesystem._windows._handles._nt_open", return_value=(0, 0x00000108), autospec=True
    )

    with pytest.raises(Timeout):
        FileLock(str(tmp_path / "a")).acquire(blocking=False)

    assert nt_open.call_count == 1


@WINDOWS_ONLY  # pragma: win32 cover
def test_windows_release_does_not_wait_for_an_oplock_taken_after_the_close(
    tmp_path: Path, hold_oplock: Callable[[Path], Callable[[], None]], mocker: MockerFixture
) -> None:
    lock_path = tmp_path / "a"
    lock = FileLock(str(lock_path), is_singleton=False, thread_local=False)
    real_close = BaseFileLock._close_released_fd

    def close_then_take_oplock(self: BaseFileLock, fd: int, *, default_suppresses: bool) -> None:
        real_close(self, fd, default_suppresses=default_suppresses)
        hold_oplock(lock_path)

    mocker.patch.object(BaseFileLock, "_close_released_fd", autospec=True, side_effect=close_then_take_oplock)
    lock.acquire()

    assert call_within(lock.release, 5) == (True, None)


@WINDOWS_ONLY  # pragma: win32 cover
@pytest.mark.parametrize("name", [pytest.param("missing", id="name"), pytest.param("missing/a", id="path")])
def test_windows_unlink_without_oplock_wait_reports_a_missing_file(tmp_path: Path, name: str) -> None:
    assert sys.platform == "win32"
    from filelock._filesystem import unlink_without_oplock_wait

    with pytest.raises(FileNotFoundError):
        unlink_without_oplock_wait(str(tmp_path / name))


@WINDOWS_ONLY  # pragma: win32 cover
def test_windows_unlink_without_oplock_wait_reports_a_file_held_without_delete_sharing(tmp_path: Path) -> None:
    assert sys.platform == "win32"
    from filelock._filesystem import unlink_without_oplock_wait

    (path := tmp_path / "a").touch()
    fd = os.open(path, os.O_RDONLY)
    try:
        with pytest.raises(PermissionError):
            unlink_without_oplock_wait(str(path))
    finally:
        os.close(fd)


@WINDOWS_ONLY  # pragma: win32 cover
def test_windows_unlink_without_oplock_wait_leaves_an_oplocked_file(
    tmp_path: Path, hold_oplock: Callable[[Path], Callable[[], None]]
) -> None:
    assert sys.platform == "win32"
    from filelock._filesystem import unlink_without_oplock_wait

    (path := tmp_path / "a").touch()
    hold_oplock(path)

    assert call_within(lambda: unlink_without_oplock_wait(str(path)), 5) == (True, None)


@WINDOWS_ONLY  # pragma: win32 cover
@NEEDS_SYMLINK  # pragma: needs symlink
def test_windows_unlink_without_oplock_wait_deletes_a_symlink_not_its_target(tmp_path: Path) -> None:
    assert sys.platform == "win32"
    from filelock._filesystem import unlink_without_oplock_wait

    (target := tmp_path / "target").touch()
    (link := tmp_path / "link").symlink_to(target)

    unlink_without_oplock_wait(str(link))

    assert (link.is_symlink(), target.exists()) == (False, True)


@WINDOWS_ONLY  # pragma: win32 cover
def test_windows_open_read_without_oplock_wait_reads_the_file(tmp_path: Path) -> None:
    assert sys.platform == "win32"
    from filelock._filesystem import open_read_without_oplock_wait

    (path := tmp_path / "a").write_bytes(b"marker")
    fd = open_read_without_oplock_wait(str(path))
    try:
        assert os.read(fd, 16) == b"marker"
    finally:
        os.close(fd)


@WINDOWS_ONLY  # pragma: win32 cover
def test_windows_open_read_without_oplock_wait_can_refuse_delete_sharing(tmp_path: Path) -> None:
    assert sys.platform == "win32"
    from filelock._filesystem import open_read_without_oplock_wait

    (path := tmp_path / "a").touch()
    fd = open_read_without_oplock_wait(str(path), share_delete=False)
    try:
        with pytest.raises(PermissionError):
            path.unlink()
    finally:
        os.close(fd)


@WINDOWS_ONLY  # pragma: win32 cover
def test_windows_open_read_without_oplock_wait_reports_a_missing_file(tmp_path: Path) -> None:
    assert sys.platform == "win32"
    from filelock._filesystem import open_read_without_oplock_wait

    with pytest.raises(FileNotFoundError):
        open_read_without_oplock_wait(str(tmp_path / "missing"))


@WINDOWS_ONLY  # pragma: win32 cover
@NEEDS_SYMLINK  # pragma: needs symlink
def test_windows_open_read_without_oplock_wait_refuses_a_symlink(tmp_path: Path) -> None:
    assert sys.platform == "win32"
    from filelock._filesystem import open_read_without_oplock_wait

    (target := tmp_path / "target").touch()
    (link := tmp_path / "link").symlink_to(target)

    with pytest.raises(OSError, match="reparse point"):
        open_read_without_oplock_wait(str(link))


@WINDOWS_ONLY  # pragma: win32 cover
@pytest.mark.parametrize("opener", ["open_read_without_oplock_wait", "open_for_delete_without_oplock_wait"])
def test_windows_open_without_oplock_wait_reports_the_break_as_contention(
    oplocked_lock_path: Path, opener: str
) -> None:
    from filelock import _filesystem as filesystem

    finished, error = call_within(lambda: getattr(filesystem, opener)(str(oplocked_lock_path)), 5)

    assert (finished, type(error)) == (True, BlockingIOError)


@WINDOWS_ONLY  # pragma: win32 cover
def test_windows_cancelling_oplock_wait_ends_a_call_that_still_waits() -> None:
    assert sys.platform == "win32"
    from filelock._filesystem import cancelling_oplock_wait

    reader, writer = os.pipe()

    def read_an_empty_pipe() -> None:
        # Waits inside the system call, as a rename does for the holder of a read-handle oplock.
        with cancelling_oplock_wait():
            os.read(reader, 1)

    finished, error = call_within(read_an_empty_pipe, 5)
    os.close(writer)  # ends the read when nothing canceled it
    os.close(reader)

    assert (finished, isinstance(error, OSError)) == (True, True)
