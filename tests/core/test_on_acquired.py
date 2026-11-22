from __future__ import annotations

import os
from errno import EIO, ENOSYS
from pathlib import Path
from stat import S_IMODE
from typing import TYPE_CHECKING, Final

import pytest

from filelock import (
    FileLock,
    SoftFileLock,
    Timeout,
)
from tests.capability_marks import (
    NEEDS_FCNTL,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from pytest_mock import MockerFixture


def test_on_acquired_defaults_none(tmp_path: Path) -> None:
    assert FileLock(str(tmp_path / "a")).on_acquired is None


def test_on_acquired_property_reflects_argument(tmp_path: Path) -> None:
    assert FileLock(str(tmp_path / "a"), on_acquired=_noop_on_acquired).on_acquired is _noop_on_acquired


def _noop_on_acquired(_fd: int) -> None:
    pass


def test_on_acquired_noop_hook_allows_acquire(tmp_path: Path) -> None:
    lock = FileLock(str(tmp_path / "a"), on_acquired=_noop_on_acquired)
    lock.acquire()
    try:
        assert lock.is_locked
    finally:
        lock.release()


def test_on_acquired_runs_before_acquire_returns(tmp_path: Path) -> None:
    fd_while_held = -1
    locked_during_hook = False

    def hook(fd: int) -> None:
        nonlocal fd_while_held, locked_during_hook
        locked_during_hook = lock.is_locked
        fd_while_held = fd

    lock = FileLock(str(tmp_path / "a"), on_acquired=hook)
    lock.acquire()
    try:
        assert locked_during_hook
        assert fd_while_held >= 0
    finally:
        lock.release()


def test_on_acquired_fires_once_per_physical_acquire(tmp_path: Path) -> None:
    calls: list[int] = []
    lock = FileLock(str(tmp_path / "a"), on_acquired=calls.append)
    lock.acquire()
    lock.acquire()
    try:
        assert len(calls) == 1
    finally:
        lock.release()
        lock.release()


def test_on_acquired_not_called_for_contender(tmp_path: Path) -> None:
    path = str(tmp_path / "a")
    holder = FileLock(path)
    holder.acquire()
    calls: list[int] = []
    contender = FileLock(path, on_acquired=calls.append)
    try:
        with pytest.raises(Timeout):
            contender.acquire(timeout=0.1)
        assert calls == []
    finally:
        holder.release()


@NEEDS_FCNTL  # pragma: needs fcntl
def test_on_acquired_runs_after_mode(tmp_path: Path) -> None:
    seen: list[int] = []

    def hook(fd: int) -> None:
        seen.append(S_IMODE(os.fstat(fd).st_mode))

    lock = FileLock(str(tmp_path / "a"), mode=0o600, on_acquired=hook)
    lock.acquire()
    try:
        assert seen == [0o600]
    finally:
        lock.release()


@NEEDS_FCNTL  # pragma: needs fcntl
def test_on_acquired_writes_survive_contention(tmp_path: Path) -> None:
    path = str(tmp_path / "a")

    def write_metadata(fd: int) -> None:
        os.write(fd, b"holder-metadata")

    writer = FileLock(path, on_acquired=write_metadata)
    writer.acquire()
    try:
        contender = FileLock(path)
        with pytest.raises(Timeout):
            contender.acquire(timeout=0.1)
        assert Path(path).read_bytes() == b"holder-metadata"
    finally:
        writer.release()


def test_on_acquired_failure_releases_lock(tmp_path: Path) -> None:
    path = str(tmp_path / "a")
    lock = FileLock(path, on_acquired=_failing_on_acquired)
    with pytest.raises(RuntimeError, match="hook failed"):
        lock.acquire()
    assert not lock.is_locked
    assert lock.lock_counter == 0
    other = FileLock(path)
    other.acquire()
    other.release()


def _failing_on_acquired(_fd: int) -> None:
    msg = "hook failed"
    raise RuntimeError(msg)


@NEEDS_FCNTL  # pragma: needs fcntl
def test_on_acquired_and_release_failure_group(tmp_path: Path, mocker: MockerFixture) -> None:
    lock = FileLock(str(tmp_path / "a"), on_acquired=_failing_on_acquired)
    mocker.patch("filelock._native._unix.unlock_fd", side_effect=[OSError(EIO, "unlock failed"), None], autospec=True)
    with pytest.raises(BaseExceptionGroup) as info:
        lock.acquire()
    assert {type(error) for error in info.value.exceptions} == {RuntimeError, OSError}
    assert lock.is_locked
    assert lock.lock_counter == 1
    lock.release()


def test_on_acquired_rollback_group_detaches_release_context(
    tmp_path: Path, close_failure: tuple[Callable[[int], None], OSError, RuntimeError]
) -> None:
    capture, release_error, release_cause = close_failure
    callback_error = RuntimeError("hook failed")
    callback_cause = LookupError("hook cause")

    def fail(fd: int) -> None:
        capture(fd)
        raise callback_error from callback_cause

    lock = FileLock(str(tmp_path / "a"), close_error_policy="raise", on_acquired=fail)
    with pytest.raises(ExceptionGroup) as info:
        lock.acquire()
    assert (
        info.value.exceptions,
        callback_error.__context__,
        release_error.__context__,
        callback_error.__cause__,
        release_error.__cause__,
        callback_error.__traceback__ is not None,
        release_error.__traceback__ is not None,
    ) == ((callback_error, release_error), None, None, callback_cause, release_cause, True, True)


@NEEDS_FCNTL  # pragma: needs fcntl
def test_on_acquired_fails_closed_on_enosys(tmp_path: Path, mocker: MockerFixture) -> None:
    mocker.patch("filelock._native._unix.fcntl.flock", side_effect=OSError(ENOSYS, "no flock"), autospec=True)
    calls: list[int] = []
    lock = FileLock(str(tmp_path / "a"), on_acquired=calls.append)
    with pytest.raises(OSError, match="no flock"):
        lock.acquire()
    assert not lock.is_locked
    assert type(lock).__name__ == "UnixFileLock"  # a soft lock cannot run the hook, so no fallback
    assert calls == []


def test_on_acquired_rejected_by_soft_lock(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="on_acquired"):
        SoftFileLock(str(tmp_path / "a"), on_acquired=_noop_on_acquired)


def test_singleton_shares_same_on_acquired(tmp_path: Path) -> None:
    path = str(tmp_path / "a")
    first = FileLock(path, is_singleton=True, on_acquired=_noop_on_acquired)
    try:
        assert FileLock(path, is_singleton=True, on_acquired=_noop_on_acquired) is first
    finally:
        first.release(force=True)


def test_singleton_rejects_different_on_acquired(tmp_path: Path) -> None:
    path = str(tmp_path / "a")
    first = FileLock(path, is_singleton=True, on_acquired=_noop_on_acquired)
    try:
        with pytest.raises(ValueError, match="on_acquired"):
            FileLock(path, is_singleton=True, on_acquired=_failing_on_acquired)
    finally:
        first.release(force=True)


@pytest.mark.parametrize("force", [False, True])
def test_hook_cannot_leave_acquire_unheld(tmp_path: Path, *, force: bool) -> None:
    path = tmp_path / "hook.lock"
    lock = FileLock(path, on_acquired=lambda _fd: lock.release(force=force))

    with pytest.raises(RuntimeError, match=r"force-released|acquire of it is still running"):
        lock.acquire(timeout=0)

    assert (lock.is_locked, lock.lock_counter) == (False, 0)
    with FileLock(path, blocking=False, is_singleton=False) as peer:
        assert peer.is_locked


def test_force_release_in_exiting_hook_preserves_exit(tmp_path: Path) -> None:
    def stop(_fd: int) -> None:
        lock.release(force=True)
        raise SystemExit(17)

    lock = FileLock(tmp_path / "exit.lock", on_acquired=stop)
    with pytest.raises(SystemExit) as exc:
        lock.acquire()

    assert (exc.value.code, lock.is_locked, lock.lock_counter) == (17, False, 0)


@pytest.mark.parametrize(
    "interrupt",
    [
        pytest.param([None, KeyboardInterrupt, None], id="record"),
        pytest.param([None, None, KeyboardInterrupt, None], id="return"),
    ],
)
def test_interrupted_nested_acquire_preserves_initial_depth(
    tmp_path: Path, mocker: MockerFixture, interrupt: list[type[KeyboardInterrupt] | None]
) -> None:
    lock = SoftFileLock(tmp_path / "nested.lock", thread_local=False)
    with lock:
        mocker.patch.object(
            type(lock._context),
            "acquiring",
            new_callable=mocker.PropertyMock,
            side_effect=interrupt,
        )
        with pytest.raises(KeyboardInterrupt):
            lock.acquire()
        mocker.stopall()
        assert (lock.is_locked, lock.lock_counter) == (True, 1)
    with SoftFileLock(lock.lock_file, blocking=False, is_singleton=False) as peer:
        assert peer.is_locked


def test_balanced_acquire_in_hook(tmp_path: Path) -> None:
    def nest(_fd: int) -> None:
        with lock:
            assert lock.lock_counter == 2

    lock = FileLock(tmp_path / "hook.lock", on_acquired=nest)
    with lock:
        assert lock.lock_counter == 1


def test_failed_proxy_releases_provisional_hold(tmp_path: Path, mocker: MockerFixture) -> None:
    lock = SoftFileLock(tmp_path / "proxy.lock")
    mocker.patch("filelock._core._lock.AcquireReturnProxy", side_effect=MemoryError, autospec=True)

    with pytest.raises(MemoryError):
        lock.acquire()
    mocker.stopall()

    assert (lock.is_locked, lock.lock_counter) == (False, 0)
    with SoftFileLock(lock.lock_file, blocking=False, is_singleton=False) as peer:
        assert peer.is_locked


def test_on_acquired_reentrant_acquire_still_registers_holder(tmp_path: Path) -> None:
    lock_path: Final[Path] = tmp_path / "a.lock"

    def reacquire(_fd: int) -> None:
        lock.acquire()

    lock: Final[SoftFileLock] = FileLock(lock_path, on_acquired=reacquire)
    with lock:
        other: Final[SoftFileLock] = FileLock(lock_path)
        # Without the registry entry the deadlock check stays silent and only the cancel check ends the wait.
        with pytest.raises(RuntimeError, match="Deadlock"):
            other.acquire(cancel_check=lambda: True)
        lock.release()
