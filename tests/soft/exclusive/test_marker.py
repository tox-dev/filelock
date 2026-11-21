from __future__ import annotations

import itertools
import os
import time
from contextlib import suppress
from multiprocessing import get_context
from types import SimpleNamespace
from typing import TYPE_CHECKING, Final

import pytest

import filelock._soft._exclusive._marker  # simplint: ignore[private-import-path]  # tests internal behavior
from filelock import MarkerSoftFileLock, OwnerRecord, SoftFileLease, Timeout
from filelock._identity import host_name, process_start_token
from filelock._soft import MALFORMED_LOCK_AGE_THRESHOLD, encode_marker
from tests.capability_marks import NEEDS_STALE_RECLAIM
from tests.process_helpers import cleanup_processes

if TYPE_CHECKING:
    from collections.abc import Callable
    from contextlib import AbstractContextManager
    from multiprocessing.process import BaseProcess
    from pathlib import Path

    from pytest_mock import MockerFixture

#: A spawned GraalPy interpreter alone can take seconds to start on a loaded runner.
_PROCESS_DEADLINE: Final[int] = 30


def test_held_lease_records_the_process_start_token(tmp_path: Path) -> None:
    with SoftFileLease(str(tmp_path / "a.lock"), lease_duration=5) as lease:
        assert lease.owner is not None
        assert lease.owner.start == process_start_token(os.getpid())


def test_marker_without_start_token_omits_it(tmp_path: Path, mocker: MockerFixture) -> None:
    # A platform without a proven start time publishes no start field, and the record still reads back cleanly.
    mocker.patch(
        "filelock._soft._exclusive._marker.process_start_token",
        new=mocker.create_autospec(filelock._soft._exclusive._marker.process_start_token, return_value=None),
    )
    marker = tmp_path / "a.lock"
    with SoftFileLease(str(marker), lease_duration=5) as lease:
        assert lease.owner is not None
        assert lease.owner.start is None
        assert "start=" not in marker.read_text(encoding="utf-8")


def test_lease_treats_an_unreadable_record_as_contention(tmp_path: Path) -> None:
    # Only a peer that published a lease agreed to be superseded by age, so an unreadable record is never reclaimed.
    marker = tmp_path / "a.lock"
    marker.write_text("filelock/2\npid=1\nhost=h\nmode=banana\n", encoding="utf-8")

    with pytest.raises(Timeout):
        SoftFileLease(str(marker), lease_duration=0.3, heartbeat_interval=0.1, timeout=0.5).acquire()

    assert marker.exists()


def test_lease_never_ages_out_an_unknown_contract(tmp_path: Path) -> None:
    # A newer marker format may grant a longer hold than this version recognizes.
    marker = tmp_path / "a.lock"
    marker.write_text("filelock/2\npid=999999\nhost=nowhere\nmode=fenced\n", encoding="utf-8")
    stale = time.time() - 3600
    os.utime(marker, (stale, stale))

    with pytest.raises(Timeout):
        SoftFileLease(str(marker), lease_duration=0.3, heartbeat_interval=0.1, timeout=0.5).acquire()

    assert marker.exists()


@NEEDS_STALE_RECLAIM
def test_lease_ages_out_a_malformed_record(tmp_path: Path, mocker: MockerFixture) -> None:
    # The self-heal still applies to a record that states no contract at all, so corruption cannot wedge contenders.
    mocker.patch(
        "filelock._soft._exclusive._recovery.time",
        new=SimpleNamespace(
            monotonic=mocker.create_autospec(
                time.monotonic, side_effect=itertools.count(step=MALFORMED_LOCK_AGE_THRESHOLD)
            ),
            sleep=time.sleep,
        ),
    )
    marker = tmp_path / "a.lock"
    marker.write_text("filelock/2\nnot-a-record\n", encoding="utf-8")

    lease = SoftFileLease(str(marker), lease_duration=5)
    _acquire_after_reclaim(lease)
    try:
        assert lease.is_lock_held_by_us
    finally:
        lease.release()


def _acquire_after_reclaim(lock: MarkerSoftFileLock) -> None:
    # Each nonblocking acquire performs one poll.
    assert any(_polled_once_and_holds(lock) for _ in range(3)), "the marker was not reclaimed within three polls"


def _polled_once_and_holds(lock: MarkerSoftFileLock) -> bool:
    with suppress(Timeout):
        lock.acquire(blocking=False)
    return lock.is_locked


def test_marker_pid_and_owner_are_none_without_marker(tmp_path: Path) -> None:
    lease = SoftFileLease(tmp_path / "a")
    assert (lease.pid, lease.owner, lease.is_lock_held_by_us) == (None, None, False)


@pytest.mark.skipif(process_start_token(os.getpid()) is None, reason="platform exposes no proven process start time")
@pytest.mark.parametrize(
    ("offset", "expected"),
    [pytest.param(0, True, id="matching-start"), pytest.param(1, False, id="recycled-pid")],
)
def test_marker_is_lock_held_by_us_checks_start(marker: Path, offset: int, *, expected: bool) -> None:
    # Same PID and host as ours; a different start token means an earlier process reused this PID.
    start = process_start_token(os.getpid())
    assert start is not None
    record = OwnerRecord(pid=os.getpid(), hostname=host_name(), mode="exclusive", start=start + offset)
    marker.write_bytes(encode_marker(record))
    assert MarkerSoftFileLock(marker).is_lock_held_by_us is expected


def test_marker_force_break_removes_the_marker(tmp_path: Path) -> None:
    # Only a closed marker can be removed on Windows.
    marker = tmp_path / "a"
    marker.write_text("filelock/2\npid=1\nhost=h\nmode=lease\n", encoding="utf-8")
    assert marker.exists()

    SoftFileLease(marker).force_break()

    assert not marker.exists()


@pytest.mark.parametrize(
    "enter",
    [pytest.param(lambda lock: lock.acquire(), id="acquire"), pytest.param(lambda lock: lock, id="context")],
)
def test_marker_lock_publishes_exclusive(
    marker: Path, enter: Callable[[MarkerSoftFileLock], AbstractContextManager[MarkerSoftFileLock]]
) -> None:
    lock: Final[MarkerSoftFileLock] = MarkerSoftFileLock(marker)
    with enter(lock):
        owner: Final[OwnerRecord | None]
        assert (owner := MarkerSoftFileLock(lock.lock_file).owner) is not None
        assert (lock.is_locked, lock.is_lock_held_by_us, lock.pid, owner.mode) == (True, True, os.getpid(), "exclusive")
    assert (lock.is_locked, lock.pid, lock.owner) == (False, None, None)


@pytest.mark.parametrize(
    "contender_type",
    [pytest.param(MarkerSoftFileLock, id="marker"), pytest.param(SoftFileLease, id="lease")],
)
def test_marker_lock_excludes_contenders(marker: Path, contender_type: type[MarkerSoftFileLock]) -> None:
    with MarkerSoftFileLock(marker):
        os.utime(marker, (0, 0))
        with pytest.raises(Timeout):
            contender_type(marker, timeout=0.1).acquire()


@pytest.mark.parametrize(
    "mode",
    [
        pytest.param("exclusive", id="exclusive"),
        pytest.param("lease", id="lease"),
        pytest.param("future", id="unknown"),
    ],
)
def test_marker_lock_preserves_live_records(marker: Path, mode: str) -> None:
    with MarkerSoftFileLock(marker):
        record: Final[str] = marker.read_text(encoding="utf-8").replace("mode=exclusive", f"mode={mode}")
    marker.write_text(f"{record}token=t\nduration=1\n", encoding="utf-8")
    os.utime(marker, (0, 0))
    with pytest.raises(Timeout):
        MarkerSoftFileLock(marker, timeout=0.1).acquire()


@pytest.mark.timeout(_PROCESS_DEADLINE * 2)
@NEEDS_STALE_RECLAIM
def test_marker_lock_reclaims_dead_owner(marker: Path) -> None:
    process: Final[BaseProcess] = get_context("spawn").Process(target=_leave_marker, args=(marker,))
    with cleanup_processes([process]):
        process.start()
        process.join(timeout=_PROCESS_DEADLINE)
        assert process.exitcode == 0
    lock = MarkerSoftFileLock(marker)
    _acquire_after_reclaim(lock)
    try:
        assert lock.pid == os.getpid()
    finally:
        lock.release()


def _leave_marker(marker: Path) -> None:
    with MarkerSoftFileLock(marker):
        os._exit(0)


@pytest.mark.timeout(_PROCESS_DEADLINE * 2)
@NEEDS_STALE_RECLAIM
def test_marker_lock_reclaims_dead_owner_before_its_process_is_closed(marker: Path) -> None:
    # Until close(), the joined Process holds the owner's handle, which on Windows keeps the exited owner openable.
    process: Final[BaseProcess] = get_context("spawn").Process(target=_leave_marker, args=(marker,))
    with cleanup_processes([process]):
        process.start()
        process.join(timeout=_PROCESS_DEADLINE)
        assert process.exitcode == 0
        lock = MarkerSoftFileLock(marker)
        _acquire_after_reclaim(lock)
        lock.release()


@pytest.mark.parametrize(
    "record",
    [
        pytest.param("broken", id="broken"),
        pytest.param("filelock/2\npid=1\nhost=h\nmode=exclu", id="truncated-mode"),
    ],
)
@NEEDS_STALE_RECLAIM
def test_marker_lock_reclaims_malformed_record(marker: Path, record: str, mocker: MockerFixture) -> None:
    mocker.patch(
        "filelock._soft._exclusive._recovery.time",
        new=SimpleNamespace(
            monotonic=mocker.create_autospec(
                time.monotonic, side_effect=itertools.count(step=MALFORMED_LOCK_AGE_THRESHOLD)
            ),
            sleep=time.sleep,
        ),
    )
    marker.write_text(record, encoding="utf-8")
    lock = MarkerSoftFileLock(marker)
    _acquire_after_reclaim(lock)
    try:
        assert lock.pid == os.getpid()
    finally:
        lock.release()
