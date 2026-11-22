from __future__ import annotations

import gc
import os
import sys
from contextlib import suppress
from errno import EACCES
from typing import TYPE_CHECKING, Final

import pytest

from filelock import CloseErrorPolicy, SoftFileLock, Timeout
from filelock._filesystem import exchange_names
from tests.capability_marks import (
    NEEDS_STALE_RECLAIM,
    NEEDS_UNLINK_OPEN_FILE,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from contextlib import AbstractContextManager
    from pathlib import Path

    from pytest_mock import MockerFixture

from tests.soft.exclusive.recovery_helpers import (
    DEAD_PID,
    WINDOWS_ONLY,
    close_after_commit,
    holder_record,
    release_over_a_successor_marker,
)


@pytest.mark.skipif(sys.platform == "win32", reason="Windows cannot exchange two names")
@pytest.mark.usefixtures("unchanged_past_grace")
@NEEDS_STALE_RECLAIM
def test_release_under_a_peers_exchange_leaves_no_marker_naming_this_process(  # pragma: win32 no cover
    lock_path: Path,
) -> None:
    lock: Final[SoftFileLock] = SoftFileLock(lock_path)
    lock.acquire()
    (peer := lock_path.with_name("peer")).write_text(holder_record(DEAD_PID), encoding="utf-8")
    exchange_names(str(lock_path), str(peer))
    lock.release()
    exchange_names(str(lock_path), str(peer))
    restored: Final[str] = lock_path.read_text(encoding="utf-8")

    contender: Final[SoftFileLock] = SoftFileLock(lock_path)
    for _ in range(2):
        with suppress(Timeout):
            contender.acquire(blocking=False)
    assert (restored.startswith(f"{os.getpid()}\n"), contender.is_locked) == (False, True)


@pytest.mark.skipif(sys.platform == "win32", reason="Windows cannot exchange two names")
@NEEDS_STALE_RECLAIM
@pytest.mark.parametrize("missing", [pytest.param({}, id="pwrite"), pytest.param({"pwrite": None}, id="no-pwrite")])
def test_release_under_a_peers_exchange_leaves_the_peers_marker(  # pragma: win32 no cover
    lock_path: Path, mocker: MockerFixture, missing: dict[str, None]
) -> None:
    mocker.patch.dict(os.__dict__, missing)
    lock: Final[SoftFileLock] = SoftFileLock(lock_path)
    lock.acquire()
    (peer := lock_path.with_name("peer")).write_text(holder_record(DEAD_PID), encoding="utf-8")
    exchange_names(str(lock_path), str(peer))
    lock.release()

    assert (lock_path.read_text(encoding="utf-8"), peer.read_text(encoding="utf-8")) == (
        holder_record(DEAD_PID),
        "filelock/released\n",
    )


def test_normal_release_removes_own_marker(lock_path: Path) -> None:
    with SoftFileLock(lock_path):
        assert lock_path.exists()
    assert not lock_path.exists()


def test_del_suppresses_a_release_error(lock_path: Path, mocker: MockerFixture) -> None:
    # Pytest reports exceptions from garbage-collected locks as unraisable.
    lock = SoftFileLock(lock_path)
    lock.acquire()
    mocker.patch.object(SoftFileLock, "release", side_effect=OSError("release failed"), autospec=True)
    del lock
    gc.collect()


@NEEDS_UNLINK_OPEN_FILE
def test_stale_release_spares_a_successor(lock_path: Path) -> None:  # pragma: needs unlink-open-file
    holder = SoftFileLock(lock_path)
    holder.acquire()
    # A peer breaks the stale marker and installs its own at the same path; unlink first so it gets a fresh inode.
    lock_path.unlink()
    lock_path.write_text(holder_record(DEAD_PID), encoding="utf-8")
    holder.release()
    assert lock_path.exists()
    assert str(DEAD_PID) in lock_path.read_text(encoding="utf-8")


@WINDOWS_ONLY
def test_windows_release_spares_a_replacement(lock_path: Path, mocker: MockerFixture) -> None:  # pragma: win32 cover
    lock = SoftFileLock(lock_path)
    lock.acquire()
    mocker.patch(
        "os.lstat",
        return_value=os.stat_result((0, 999, 1, 0, 0, 0, 0, 0, 0, 0)),
        autospec=True,
    )
    lock.release()
    mocker.stopall()
    assert lock_path.exists()
    lock_path.unlink()


@pytest.mark.parametrize("policy", ["default", "raise"])
def test_close_error_policy_cleans_marker(lock_path: Path, mocker: MockerFixture, policy: CloseErrorPolicy) -> None:
    lock = SoftFileLock(lock_path, close_error_policy=policy)
    lock.acquire()
    with close_after_commit(mocker, lock) as (close_error, attempts):
        with pytest.raises(OSError, match="close failed") as info:
            lock.release()
        assert info.value is close_error
    assert (len(attempts), lock.is_locked, lock.lock_counter, lock_path.exists()) == (1, False, 0, False)


def test_close_error_suppress_policy_cleans_marker(lock_path: Path, mocker: MockerFixture) -> None:
    lock = SoftFileLock(lock_path, close_error_policy="suppress")
    lock.acquire()
    with close_after_commit(mocker, lock) as (_, attempts):
        lock.release()
    assert (len(attempts), lock.is_locked, lock.lock_counter, lock_path.exists()) == (1, False, 0, False)


def test_close_error_suppression_releases_nested_acquisitions_once(lock_path: Path, mocker: MockerFixture) -> None:
    lock = SoftFileLock(lock_path, close_error_policy="suppress")
    lock.acquire()
    lock.acquire()
    with close_after_commit(mocker, lock) as (_, attempts):
        lock.release()
        lock.release()
    assert (len(attempts), lock.is_locked, lock.lock_counter, lock_path.exists()) == (1, False, 0, False)


def test_close_error_suppression_forced_release_releases_once(lock_path: Path, mocker: MockerFixture) -> None:
    lock = SoftFileLock(lock_path, close_error_policy="suppress")
    lock.acquire()
    lock.acquire()
    with close_after_commit(mocker, lock) as (_, attempts):
        lock.release(force=True)
    assert (len(attempts), lock.is_locked, lock.lock_counter, lock_path.exists()) == (1, False, 0, False)


@pytest.mark.parametrize(
    "enter",
    [pytest.param(lambda lock: lock, id="direct"), pytest.param(SoftFileLock.acquire, id="proxy")],
)
def test_context_surfaces_close_error_after_cleanup(
    lock_path: Path,
    mocker: MockerFixture,
    enter: Callable[[SoftFileLock], AbstractContextManager[SoftFileLock]],
) -> None:
    lock = SoftFileLock(lock_path)
    with (
        close_after_commit(mocker, lock) as (close_error, attempts),
        pytest.raises(OSError, match="close failed") as info,
        enter(lock),
    ):
        pass
    assert (info.value, len(attempts), lock.is_locked, lock_path.exists()) == (close_error, 1, False, False)


def test_second_release_does_not_close_reused_descriptor(
    lock_path: Path,
    tmp_path: Path,
    mocker: MockerFixture,
) -> None:
    lock = SoftFileLock(lock_path)
    lock.acquire()
    with (
        close_after_commit(mocker, lock) as (close_error, attempts),
        pytest.raises(OSError, match="close failed") as info,
    ):
        lock.release()
    assert info.value is close_error
    reused_fd = os.open(tmp_path / "reused", os.O_CREAT | os.O_WRONLY)
    assert reused_fd == attempts[0]
    try:
        lock.release()
        os.fstat(reused_fd)
    finally:
        os.close(reused_fd)


@NEEDS_UNLINK_OPEN_FILE  # pragma: needs unlink-open-file
def test_close_error_spares_successor_marker(lock_path: Path, mocker: MockerFixture) -> None:
    holder = SoftFileLock(lock_path)
    holder.acquire()
    lock_path.unlink()
    lock_path.write_text(holder_record(DEAD_PID), encoding="utf-8")
    with close_after_commit(mocker, holder) as (_, _attempts), pytest.raises(OSError, match="close failed"):
        holder.release()
    assert lock_path.read_text(encoding="utf-8") == holder_record(DEAD_PID)


@NEEDS_UNLINK_OPEN_FILE  # pragma: needs unlink-open-file
def test_released_record_write_error_spares_successor_marker(lock_path: Path, mocker: MockerFixture) -> None:
    holder: Final[SoftFileLock] = release_over_a_successor_marker(lock_path)
    mocker.patch(
        "filelock._soft._exclusive._publication.write_at_start",
        side_effect=PermissionError(EACCES, "record denied"),
        autospec=True,
    )

    holder.release()

    assert not holder.is_locked
    assert lock_path.read_text(encoding="utf-8") == holder_record(DEAD_PID)


@NEEDS_UNLINK_OPEN_FILE  # pragma: needs unlink-open-file
@pytest.mark.parametrize(
    "operation",
    [
        pytest.param("filelock._soft._exclusive._publication.write_at_start", id="write"),
        pytest.param("os.ftruncate", id="truncate"),
    ],
)
def test_displaced_marker_can_retry_release_after_timeout(
    lock_path: Path, mocker: MockerFixture, operation: str
) -> None:
    holder = SoftFileLock(lock_path)
    holder.acquire()
    displaced = lock_path.with_suffix(".aside")
    lock_path.rename(displaced)
    lock_path.write_text(holder_record(DEAD_PID), encoding="utf-8")
    timeout = mocker.patch(operation, side_effect=TimeoutError("release deadline"), autospec=True)

    with pytest.raises(TimeoutError, match="release deadline"):
        holder.release()
    assert holder.is_locked

    mocker.stop(timeout)
    holder.release()
    assert not holder.is_locked
    assert displaced.read_bytes() == b"filelock/released\n"
    assert lock_path.read_text(encoding="utf-8") == holder_record(DEAD_PID)
