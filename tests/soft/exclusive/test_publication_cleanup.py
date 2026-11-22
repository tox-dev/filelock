from __future__ import annotations

import os
import signal
import sys
import threading
import time
from errno import EACCES, EINTR, EIO, ENOSPC
from pathlib import Path
from types import FrameType, SimpleNamespace
from typing import TYPE_CHECKING, Final

import pytest

import filelock._soft._exclusive._publication  # simplint: ignore[private-import-path]  # tests internal behavior
from filelock import SoftFileLease, SoftFileLock
from filelock._core import FileLockContext
from tests.capability_marks import (
    NEEDS_POSIX_SIGNALS,
    NEEDS_PROMPT_SIGNAL_DELIVERY,
    NEEDS_STALE_RECLAIM,
    NEEDS_UNLINK_OPEN_FILE,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from pytest_mock import MockerFixture

from tests.soft.exclusive.recovery_helpers import (
    DEAD_PID,
    close_after_commit,
    hold_reports_foreign_identity,
    holder_record,
    release_over_a_successor_marker,
)


@NEEDS_UNLINK_OPEN_FILE  # pragma: needs unlink-open-file
def test_released_record_rewrite_error_spares_successor_marker_without_pwrite(
    lock_path: Path, mocker: MockerFixture
) -> None:
    holder: Final[SoftFileLock] = release_over_a_successor_marker(lock_path)
    mocker.patch("os.pwrite", new=None, create=True)
    mocker.patch(
        "filelock._filesystem._files.os.write",
        side_effect=PermissionError(EACCES, "record denied"),
        autospec=True,
    )

    holder.release()

    assert not holder.is_locked
    assert lock_path.read_text(encoding="utf-8") == holder_record(DEAD_PID)


@NEEDS_UNLINK_OPEN_FILE  # pragma: needs unlink-open-file
def test_release_finishes_when_marker_disappears_before_unlink(lock_path: Path, mocker: MockerFixture) -> None:
    lock = SoftFileLock(lock_path)
    lock.acquire()
    unlink = Path.unlink

    def remove_before_unlink(path: Path, *, missing_ok: bool = False) -> None:
        unlink(path)
        unlink(path, missing_ok=missing_ok)

    mocker.patch.object(Path, "unlink", autospec=True, side_effect=remove_before_unlink)
    lock.release()
    assert (lock.is_locked, lock_path.exists()) == (False, False)


@NEEDS_UNLINK_OPEN_FILE  # pragma: needs unlink-open-file
@pytest.mark.parametrize(
    "share_refusal", [pytest.param(False, id="before-close"), pytest.param(True, id="after-close")]
)
def test_release_propagates_deadline_after_unlink(
    lock_path: Path, mocker: MockerFixture, *, share_refusal: bool
) -> None:
    lock = SoftFileLock(lock_path)
    lock.acquire()
    unlink = Path.unlink

    def unlink_then_signal(path: Path, *, missing_ok: bool = False) -> None:
        nonlocal share_refusal
        if share_refusal:
            share_refusal = False
            raise PermissionError(EACCES, "file in use")
        unlink(path, missing_ok=missing_ok)
        raise TimeoutError(EINTR, "deadline")

    mocker.patch.object(Path, "unlink", autospec=True, side_effect=unlink_then_signal)
    with pytest.raises(TimeoutError, match="deadline"):
        lock.release()
    assert (lock.is_locked, lock_path.exists()) == (False, False)


@pytest.mark.skipif(sys.platform == "win32", reason="Windows closes the descriptor before it can unlink the marker")
@pytest.mark.parametrize(
    "cleanup_error",
    [pytest.param(RuntimeError("cleanup failed"), id="handler"), pytest.param(OSError(EIO, "cleanup failed"), id="io")],
)
def test_marker_cleanup_failure_keeps_the_lock_held_for_a_retry(  # pragma: win32 no cover
    lock_path: Path, mocker: MockerFixture, cleanup_error: Exception
) -> None:
    lock: Final[SoftFileLock] = SoftFileLock(lock_path)
    lock.acquire()
    unlink = mocker.patch("pathlib.Path.unlink", autospec=True, side_effect=cleanup_error)
    with pytest.raises(type(cleanup_error)) as info:
        lock.release()
    held: Final[tuple[bool, bool]] = (lock.is_locked, lock_path.exists())
    mocker.stop(unlink)

    lock.release()

    assert (info.value, held, lock.is_locked, lock_path.exists()) == (cleanup_error, (True, True), False, False)


@pytest.mark.parametrize("lock_type", [pytest.param(SoftFileLock, id="soft"), pytest.param(SoftFileLease, id="lease")])
def test_release_keeps_marker_cleanup_after_fstat_signal(
    lock_path: Path, mocker: MockerFixture, lock_type: type[SoftFileLock]
) -> None:
    lock = lock_type(lock_path, is_singleton=False)
    lock.acquire()
    fstat = mocker.patch("os.fstat", autospec=True, side_effect=TimeoutError("deadline"))
    lock.release()
    mocker.stop(fstat)

    with SoftFileLock(lock_path, blocking=False, is_singleton=False) as peer:
        assert peer.is_locked


@NEEDS_STALE_RECLAIM
@NEEDS_POSIX_SIGNALS  # pragma: needs posix-signals
@NEEDS_PROMPT_SIGNAL_DELIVERY  # pragma: needs prompt-signal-delivery
@pytest.mark.parametrize("thread_local", [pytest.param(True, id="thread_local"), pytest.param(False, id="shared")])
def test_signal_handler_can_exit_during_reclaimed_marker_publication(
    lock_path: Path, monkeypatch: pytest.MonkeyPatch, *, thread_local: bool
) -> None:
    lock_path.write_text(holder_record(DEAD_PID), encoding="utf-8")
    lock = SoftFileLock(lock_path, is_singleton=False, thread_local=thread_local)
    released: list[bool] = []

    def release_in_handler(_signum: int, _frame: FrameType | None) -> None:
        released.append(lock.is_locked)
        lock.release(force=True)
        raise SystemExit(7)

    def signal_after_descriptor(
        context: FileLockContext,
        name: str,
        value: object,  # simplint: ignore[imprecise-annotation]  # context fields have different value types
    ) -> None:
        super(FileLockContext, context).__setattr__(name, value)
        if context is lock._hold_state() and name == "lock_file_fd" and value is not None and not released:
            signal.raise_signal(signal_number)

    signal_number = signal.Signals["SIGUSR1"]
    previous = signal.signal(signal_number, release_in_handler)
    monkeypatch.setattr(FileLockContext, "__setattr__", signal_after_descriptor)
    try:
        with pytest.raises(SystemExit) as exit_info:
            lock.acquire(timeout=1)
    finally:
        monkeypatch.undo()
        signal.signal(signal_number, previous)
    assert (exit_info.value.code, released, lock.is_locked, lock.lock_counter) == (7, [True], False, 0)
    with SoftFileLock(lock_path, blocking=False, is_singleton=False) as peer:
        assert peer.is_locked


@pytest.mark.parametrize("thread_local", [pytest.param(True, id="thread_local"), pytest.param(False, id="shared")])
def test_force_release_on_exit_during_identity_publication(
    lock_path: Path, monkeypatch: pytest.MonkeyPatch, *, thread_local: bool
) -> None:
    lock = SoftFileLock(lock_path, is_singleton=False, thread_local=thread_local)
    released: list[bool] = []

    def release_after_descriptor(
        context: FileLockContext,
        name: str,
        value: object,  # simplint: ignore[imprecise-annotation]  # context fields have different value types
    ) -> None:
        super(FileLockContext, context).__setattr__(name, value)
        if context is lock._hold_state() and name == "lock_file_fd" and value is not None and not released:
            released.append(lock.is_locked)
            lock.release(force=True)
            raise SystemExit(7)

    with monkeypatch.context() as patch:
        patch.setattr(FileLockContext, "__setattr__", release_after_descriptor)
        with pytest.raises(SystemExit) as exit_info:
            lock.acquire(timeout=1)

    assert (exit_info.value.code, released, lock.is_locked, lock.lock_counter) == (7, [True], False, 0)
    with SoftFileLock(lock_path, blocking=False, is_singleton=False) as peer:
        assert peer.is_locked


@pytest.mark.skipif(sys.platform == "win32", reason="Windows always unlinks the marker after the close")
def test_marker_unlink_refused_while_open_is_retried_after_the_close(  # pragma: win32 no cover
    lock_path: Path, mocker: MockerFixture
) -> None:
    lock: Final[SoftFileLock] = SoftFileLock(lock_path)
    lock.acquire()
    real_unlink = Path.unlink
    attempts: Final[list[Path]] = []

    def refuse_while_open(path: Path, missing_ok: bool = False) -> None:
        attempts.append(path)
        if len(attempts) == 1:
            raise PermissionError(EACCES, "file in use")
        real_unlink(path, missing_ok=missing_ok)

    mocker.patch.object(Path, "unlink", autospec=True, side_effect=refuse_while_open)
    lock.release()

    assert (lock.is_locked, lock_path.exists(), len(attempts)) == (False, False, 2)


@pytest.mark.skipif(sys.platform == "win32", reason="Windows always unlinks the marker after the close")
def test_marker_unlink_retried_after_the_close_spares_a_successor(  # pragma: win32 no cover
    lock_path: Path, mocker: MockerFixture
) -> None:
    lock: Final[SoftFileLock] = SoftFileLock(lock_path)
    lock.acquire()

    def refuse_while_open_and_successor_replaces(path: Path, missing_ok: bool = False) -> None:
        (successor := path.with_name("successor")).write_text("live", encoding="utf-8")
        successor.replace(path)
        raise PermissionError(EACCES, f"file in use, missing_ok={missing_ok}")

    mocker.patch.object(Path, "unlink", autospec=True, side_effect=refuse_while_open_and_successor_replaces)
    lock.release()
    mocker.stopall()

    assert (lock.is_locked, lock_path.read_text(encoding="utf-8")) == (False, "live")


@pytest.mark.skipif(sys.platform != "win32", reason="POSIX unlinks the marker before the close, so both cannot fail")
def test_close_and_marker_cleanup_failures_are_grouped(  # pragma: win32 cover
    lock_path: Path, mocker: MockerFixture
) -> None:
    lock = SoftFileLock(lock_path)
    lock.acquire()
    cleanup_error = RuntimeError("cleanup failed")
    unlink_mock = mocker.patch(
        "filelock._soft._exclusive._publication.unlink_path",
        new=mocker.create_autospec(filelock._soft._exclusive._publication.unlink_path, side_effect=cleanup_error),
    )
    with (
        close_after_commit(mocker, lock) as (close_error, _attempts),
        pytest.RaisesGroup(OSError, RuntimeError) as info,
    ):
        lock.release()
    mocker.stop(unlink_mock)
    assert (
        info.value.message,
        info.value.exceptions,
        close_error.__context__,
        cleanup_error.__context__,
        lock.is_locked,
    ) == (
        "lock descriptor close and marker cleanup both failed",
        (close_error, cleanup_error),
        None,
        None,
        False,
    )


@pytest.mark.parametrize(
    ("replace", "replaced"),
    [
        pytest.param(lambda _mocker: None, False, id="own-marker-removed"),
        pytest.param(hold_reports_foreign_identity, True, id="replacement-spared"),
    ],
)
def test_publication_rollback_close_failure_unlinks_only_its_marker(
    lock_path: Path, mocker: MockerFixture, replace: Callable[[MockerFixture], None], *, replaced: bool
) -> None:
    mocker.patch(
        "filelock._filesystem._files.os.write",
        side_effect=OSError(ENOSPC, "No space left on device"),
        autospec=True,
    )
    replace(mocker)
    lock: Final[SoftFileLock] = SoftFileLock(lock_path)
    with (
        close_after_commit(mocker, lock) as (close_error, attempts),
        pytest.raises(OSError, match="close failed") as info,
    ):
        lock.acquire(timeout=0)
    assert (info.value, len(attempts), lock.is_locked, lock_path.exists()) == (close_error, 1, False, replaced)


def test_close_after_commit_ignores_other_descriptors(lock_path: Path, mocker: MockerFixture) -> None:
    # A concurrent finalizer can close an unrelated descriptor through the same patch.
    lock = SoftFileLock(lock_path)
    lock.acquire()
    closed: list[int] = []

    def close_a_pipe() -> None:
        read_fd, write_fd = os.pipe()
        os.close(read_fd)
        os.close(write_fd)
        closed.extend((read_fd, write_fd))

    with close_after_commit(mocker, lock) as (_close_error, attempts):
        close_a_pipe()
        worker = threading.Thread(target=close_a_pipe)
        worker.start()
        worker.join()
        with pytest.raises(OSError, match="close failed"):
            lock.release()

    assert (len(closed), len(attempts), attempts[0] in closed) == (4, 1, False)


def test_soft_windows_unlink_gives_up_after_every_attempt_is_denied(tmp_path: Path, mocker: MockerFixture) -> None:
    # Windows can temporarily deny deletion after close.
    from filelock._soft._exclusive._publication import file_identity

    marker = tmp_path / "a"
    marker.write_text("x", encoding="utf-8")
    lock = SoftFileLock(marker)
    mocker.patch(
        "filelock._soft._exclusive._publication.time",
        new=SimpleNamespace(monotonic=time.monotonic, sleep=mocker.create_autospec(time.sleep)),
    )
    unlink = mocker.patch(
        "filelock._soft._exclusive._publication.unlink_path",
        new=mocker.create_autospec(
            filelock._soft._exclusive._publication.unlink_path,
            side_effect=PermissionError(EACCES, "handle still open"),
        ),
    )

    lock._windows_unlink_if_ours(file_identity(os.lstat(marker)))
    denied: Final[bool] = marker.exists()
    mocker.stop(unlink)

    # The marker is still ours, so the next acquire finishes the cleanup instead of waiting on it.
    with lock.acquire(timeout=0):
        assert denied
