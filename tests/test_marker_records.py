from __future__ import annotations

import itertools
import os
import time
from multiprocessing import get_context
from typing import TYPE_CHECKING, Final

import pytest

from filelock import MarkerSoftFileLock, OwnerRecord, SoftFileLease, Timeout
from filelock._identity import host_name, process_start_token
from filelock._marker import encode_marker
from filelock._soft import MALFORMED_LOCK_AGE_THRESHOLD
from tests.process_helpers import cleanup_processes

if TYPE_CHECKING:
    from multiprocessing.process import BaseProcess
    from pathlib import Path

    from pytest_mock import MockerFixture

#: A spawned GraalPy interpreter alone can take seconds to start on a loaded runner.
_PROCESS_DEADLINE: Final[int] = 30


@pytest.mark.parametrize(
    "record",
    [
        pytest.param("filelock/1\npid=1\nhost=h\nmode=lease\n", id="wrong-protocol"),
        pytest.param("filelock/2\npid=1\nhost=h\n", id="no-mode"),
        pytest.param("filelock/2\npid=1\nmode=lease\n", id="no-host"),
        pytest.param("filelock/2\npid=1\nhost=\nmode=lease\n", id="empty-host"),
        pytest.param("filelock/2\nhost=h\nmode=lease\n", id="no-pid"),
        pytest.param("filelock/2\npid=nine\nhost=h\nmode=lease\n", id="pid-not-a-number"),
        pytest.param("filelock/2\npid=0\nhost=h\nmode=lease\n", id="pid-below-range"),
        pytest.param("filelock/2\npid=99999999999\nhost=h\nmode=lease\n", id="pid-above-range"),
        pytest.param("filelock/2\npid=1\nhost=h\nmode=lease\nbare-line\n", id="field-without-value"),
        pytest.param("filelock/2\npid=1\nhost=h\nmode=lease\nduration=5\n", id="lease-without-token"),
        pytest.param("filelock/2\npid=1\nhost=h\nmode=lease\ntoken=t\n", id="lease-without-duration"),
        pytest.param("filelock/2\npid=1\nhost=h\nmode=lease\ntoken=t\nduration=0\n", id="lease-duration-zero"),
        pytest.param(
            "filelock/2\npid=1\nhost=h\nmode=lease\ntoken=t\nduration=soon\n", id="lease-duration-not-a-number"
        ),
        pytest.param("filelock/2\npid=1\nhost=h\nmode=lease\ntoken=t\nduration=nan\n", id="lease-duration-nan"),
        pytest.param("filelock/2\npid=1\nhost=h\nmode=lease\ntoken=t\nduration=inf\n", id="lease-duration-inf"),
        pytest.param("filelock/2\npid=1\nhost=h\nmode=lease\ntoken=t\nduration=5\nstart=later\n", id="start-nan"),
        pytest.param("filelock/2\npid=1\nhost=h\nmode=exclu", id="truncated-mode"),
        pytest.param("filelock/2\npid=1\nhost=h\nmode=lease\ntoken=t\nduration=1", id="truncated-duration"),
        pytest.param("filelock/2\npid=1_000\nhost=h\nmode=exclusive\n", id="pid-underscore"),
        pytest.param("filelock/2\npid=\u0661\u0662\u0663\nhost=h\nmode=exclusive\n", id="pid-arabic-indic-digits"),
        pytest.param("filelock/2\npid= 1\nhost=h\nmode=exclusive\n", id="pid-whitespace"),
        pytest.param("filelock/2\npid=+1\nhost=h\nmode=exclusive\n", id="pid-sign"),
        pytest.param("filelock/2\npid=1\nhost=h\nmode=exclusive\nstart=1_0\n", id="start-underscore"),
        pytest.param("filelock/2\npid=1\nhost=h\nmode=lease\ntoken=t\nduration= 1\n", id="duration-whitespace"),
        pytest.param("filelock/2\npid=1\nhost=h\nmode=lease\ntoken=t\nduration=1_0\n", id="duration-underscore"),
        pytest.param("filelock/2\npid=1\nhost=h\nmode=lease\ntoken=t\nduration=\u0665\n", id="duration-non-ascii"),
        pytest.param("filelock/2\npid=1\nhost=h\nmode=lease\ntoken=t\nduration=-1.0\n", id="duration-negative"),
        pytest.param("filelock/2\npid=1\nhost=h\nmode=lease\ntoken=t\nduration=1e400\n", id="duration-overflow"),
        pytest.param("filelock/2\npid=1\nhost=h\nmode=lease\ntoken=\nduration=1\n", id="lease-empty-token"),
    ],
)
def test_unreadable_record_names_no_owner(tmp_path: Path, record: str) -> None:
    marker = tmp_path / "a.lock"
    marker.write_text(record, encoding="utf-8")

    assert SoftFileLease(str(marker), lease_duration=1).owner is None


def test_encode_marker_omits_absent_lease_fields() -> None:
    # A non-lease owner carries no token or duration, so those lines never appear in the rendered marker.
    rendered = encode_marker(OwnerRecord(pid=7, hostname="h", mode="unknown"))

    assert rendered == b"filelock/2\npid=7\nhost=h\nmode=unknown\n"
    assert b"token=" not in rendered
    assert b"duration=" not in rendered


@pytest.mark.parametrize("duration", [0.9, 30, 30.0, 1e-05, 1.5e20])
def test_record_duration_reads_back(marker: Path, duration: float) -> None:
    record = OwnerRecord(4242, "h", "lease", "t", duration)
    marker.write_bytes(encode_marker(record))

    assert SoftFileLease(str(marker), lease_duration=1).owner == record


def test_record_start_token_reads_back(tmp_path: Path) -> None:
    marker = tmp_path / "a.lock"
    record = "filelock/2\npid=4242\nhost=somehost\nmode=lease\ntoken=t\nduration=5\nstart=987654\n"
    marker.write_text(record, encoding="utf-8")

    owner = SoftFileLease(str(marker), lease_duration=1).owner

    assert owner is not None
    assert (owner.pid, owner.hostname, owner.mode, owner.start) == (4242, "somehost", "lease", 987654)


def test_record_from_a_newer_filelock_still_names_its_owner(tmp_path: Path) -> None:
    # An unknown key is a field this version does not model yet; the owner it publishes must still read back.
    marker = tmp_path / "a.lock"
    record = "filelock/2\npid=4242\nhost=somehost\nmode=lease\ntoken=t\nduration=5\nstart-id=17\n"
    marker.write_text(record, encoding="utf-8")

    owner = SoftFileLease(str(marker), lease_duration=1).owner

    assert owner is not None
    assert (owner.pid, owner.hostname, owner.mode) == (4242, "somehost", "lease")


@pytest.mark.parametrize("published", ["strict", "banana"])
def test_record_with_an_unknown_mode_names_its_owner(tmp_path: Path, published: str) -> None:
    # A mode this version does not implement states a contract a newer filelock published, not a corrupt record.
    marker = tmp_path / "a.lock"
    marker.write_text(f"filelock/2\npid=4242\nhost=somehost\nmode={published}\n", encoding="utf-8")

    owner = SoftFileLease(str(marker), lease_duration=1).owner

    assert owner is not None
    assert (owner.pid, owner.hostname, owner.mode) == (4242, "somehost", "unknown")


def test_held_lease_records_the_process_start_token(tmp_path: Path) -> None:
    marker = tmp_path / "a.lock"
    with SoftFileLease(str(marker), lease_duration=5) as lease:
        assert lease.owner is not None
        assert lease.owner.start == process_start_token(os.getpid())


def test_marker_without_start_token_omits_it(tmp_path: Path, mocker: MockerFixture) -> None:
    # A platform without a proven start time publishes no start field, and the record still reads back cleanly.
    mocker.patch("filelock._marker.process_start_token", return_value=None)
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
    # A marker naming a contract this version cannot interpret must outlive the malformed grace window: aging it out
    # would delete a lock whose owner never agreed to expire.
    marker = tmp_path / "a.lock"
    marker.write_text("filelock/2\npid=999999\nhost=nowhere\nmode=fenced\n", encoding="utf-8")
    stale = time.time() - 3600
    os.utime(marker, (stale, stale))

    with pytest.raises(Timeout):
        SoftFileLease(str(marker), lease_duration=0.3, heartbeat_interval=0.1, timeout=0.5).acquire()

    assert marker.exists()


def test_lease_ages_out_a_malformed_record(tmp_path: Path, mocker: MockerFixture) -> None:
    # The self-heal still applies to a record that states no contract at all, so corruption cannot wedge contenders.
    mocker.patch("filelock._soft.time.monotonic", side_effect=itertools.count(step=MALFORMED_LOCK_AGE_THRESHOLD))
    marker = tmp_path / "a.lock"
    marker.write_text("filelock/2\nnot-a-record\n", encoding="utf-8")

    with SoftFileLease(str(marker), lease_duration=5, timeout=1) as lease:
        assert lease.is_lock_held_by_us


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
    # force_break clears a marker whose holder is gone, so the file is on disk but not held open — the one case where
    # an unlink also succeeds on Windows, which refuses to remove a descriptor another handle still holds.
    marker = tmp_path / "a"
    marker.write_text("filelock/2\npid=1\nhost=h\nmode=lease\n", encoding="utf-8")
    assert marker.exists()

    SoftFileLease(marker).force_break()

    assert not marker.exists()


@pytest.mark.parametrize("acquire", [pytest.param(True, id="acquire"), pytest.param(False, id="context")])
def test_marker_lock_publishes_exclusive(marker: Path, *, acquire: bool) -> None:
    lock: Final[MarkerSoftFileLock] = MarkerSoftFileLock(marker)
    with lock.acquire() if acquire else lock:
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
    marker.write_text(record + "token=t\nduration=1\n", encoding="utf-8")
    os.utime(marker, (0, 0))
    with pytest.raises(Timeout):
        MarkerSoftFileLock(marker, timeout=0.1).acquire()


@pytest.mark.timeout(_PROCESS_DEADLINE * 2)
def test_marker_lock_reclaims_dead_owner(marker: Path) -> None:
    process: Final[BaseProcess] = get_context("spawn").Process(target=_leave_marker, args=(marker,))
    with cleanup_processes([process]):
        process.start()
        process.join(timeout=_PROCESS_DEADLINE)
        assert process.exitcode == 0
    with MarkerSoftFileLock(marker, timeout=1) as lock:
        assert lock.pid == os.getpid()


@pytest.mark.parametrize(
    "record",
    [
        pytest.param("broken", id="broken"),
        pytest.param("filelock/2\npid=1\nhost=h\nmode=exclu", id="truncated-mode"),
    ],
)
def test_marker_lock_reclaims_malformed_record(marker: Path, record: str, mocker: MockerFixture) -> None:
    mocker.patch("filelock._soft.time.monotonic", side_effect=itertools.count(step=MALFORMED_LOCK_AGE_THRESHOLD))
    marker.write_text(record, encoding="utf-8")
    with MarkerSoftFileLock(marker, timeout=1) as lock:
        assert lock.pid == os.getpid()


@pytest.fixture
def marker(tmp_path: Path) -> Path:
    return tmp_path / "a"


def _leave_marker(marker: Path) -> None:
    with MarkerSoftFileLock(marker):
        os._exit(0)
