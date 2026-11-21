from __future__ import annotations

import sys
import time
from errno import EIO
from functools import partial
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Final

import pytest
from capabilities import CAPABILITIES

from filelock import SoftFileLockProtocolError
from filelock._soft._read_write import _heartbeat as heartbeat_mod
from filelock._soft._read_write._generations import GenerationLog, Snapshot
from filelock._soft._read_write._storage import OsFiles

from .conftest import make_participant

if TYPE_CHECKING:
    from pytest_mock import MockerFixture

pytestmark: Final[pytest.MarkDecorator] = pytest.mark.usefixtures("flushes")


def test_leave_without_entering_only_drops_the_record(lock_file: str, files: OsFiles) -> None:
    participant = make_participant(files, lock_file, "read", stale_threshold=1)
    participant.publish()
    assert Path(f"{lock_file}.rw", "holders", participant.token).exists()
    participant.leave()
    assert not Path(f"{lock_file}.rw", "holders", participant.token).exists()
    assert participant.generation is None


@pytest.mark.requires_hard_links
def test_leave_retries_a_commit_a_peer_won(lock_file: str, files: OsFiles, root: str, mocker: MockerFixture) -> None:
    # A peer can publish between the holder reading the log and linking its next generation.
    reader = make_participant(files, lock_file, "read")
    reader.publish()
    assert reader.advance()
    real_commit = GenerationLog.commit
    lost = []
    pauses: list[float] = []
    mocker.patch("filelock._soft._read_write._participant.sleep", autospec=True, side_effect=pauses.append)

    def commit_after_a_peer(log: GenerationLog, successor: Snapshot) -> bool:
        if len(lost) < 6:
            lost.append(successor)
            (peer := GenerationLog(files, lock_file, root)).latest()
            still_reading = successor.readers | {reader.token}
            assert real_commit(peer, Snapshot(generation=successor.generation, writer=None, readers=still_reading))
        return real_commit(log, successor)

    mocker.patch.object(GenerationLog, "commit", autospec=True, side_effect=commit_after_a_peer)
    reader.leave()
    latest = GenerationLog(files, lock_file, root).latest()
    assert (latest.members, latest.generation) == (frozenset(), lost[-1].generation + 1)
    assert pauses == [0.001, 0.002, 0.004, 0.008, 0.01, 0.01]


@pytest.mark.requires_hard_links
def test_leave_stops_retrying_after_stale_threshold(
    lock_file: str, files: OsFiles, root: str, mocker: MockerFixture
) -> None:
    now = 0.0

    def clock() -> float:
        return now

    reader = make_participant(files, lock_file, "read", stale_threshold=2, clock=clock)
    reader.publish()
    assert reader.advance()
    lost = []

    def lose_commit(_log: GenerationLog, successor: Snapshot) -> bool:
        nonlocal now
        lost.append(successor)
        now += 1
        return False

    mocker.patch.object(GenerationLog, "commit", autospec=True, side_effect=lose_commit)
    with pytest.raises(SoftFileLockProtocolError, match="could not withdraw holder") as caught:
        reader.leave()

    assert caught.value.lock_file == lock_file
    assert len(lost) == 2
    assert not Path(root, "holders", reader.token).exists()
    assert reader.generation is None


@pytest.mark.requires_hard_links
def test_waiting_contender_republishes_a_swept_record(lock_file: str, files: OsFiles, root: str) -> None:
    writer = make_participant(files, lock_file, "write")
    writer.publish()
    assert writer.advance()
    reader = make_participant(files, lock_file, "read")
    reader.publish()
    record = Path(root, "holders", reader.token)
    record.unlink()
    assert not reader.advance()
    assert record.exists()


@pytest.mark.requires_hard_links
def test_sweep_cannot_remove_a_resumed_waiters_hold(
    lock_file: str, files: OsFiles, root: str, mocker: MockerFixture
) -> None:
    clock = [0.0]
    holder = make_participant(files, lock_file, "write", stale_threshold=10, clock=lambda: clock[0])
    holder.publish()
    assert holder.advance()
    waiter = make_participant(files, lock_file, "write", stale_threshold=10, clock=lambda: clock[0])
    waiter.publish()
    assert not waiter.advance()
    sweeper = make_participant(files, lock_file, "write", stale_threshold=1, clock=lambda: clock[0])
    sweeper.publish()
    assert not sweeper.advance()
    judged = str(Path(root, "holders", waiter.token))
    real_unlink = files.unlink
    granted: list[bool] = []

    def unlink_after_waiter_resumes(path: str) -> None:
        if path == judged and not granted:
            granted.append(False)
            holder.leave()
            granted[0] = waiter.advance()
        real_unlink(path)

    mocker.patch.object(files, "unlink", autospec=True, side_effect=unlink_after_waiter_resumes)
    clock[0] = 10.0
    assert not sweeper.advance()
    assert granted == [True]
    assert Path(root, "holders", waiter.token).exists()
    assert waiter.heartbeat() == ("ok", None)
    clock[0] = 12.0
    assert not sweeper.advance()
    assert GenerationLog(files, lock_file, root).latest().writer == waiter.token


@pytest.mark.requires_hard_links
@pytest.mark.skipif(not CAPABILITIES["suspend-clock"], reason="needs a clock that counts host suspension")
def test_host_suspend_renews_a_waiters_token(
    lock_file: str, files: OsFiles, root: str, mocker: MockerFixture, suspended_host: tuple[list[float], list[float]]
) -> None:  # pragma: needs suspend-clock
    host_elapsed, host_monotonic = suspended_host
    local_writer = partial(
        make_participant,
        files,
        lock_file,
        "write",
        stale_threshold=10,
        clock=lambda: host_monotonic[0],
        lease_clock=heartbeat_mod.lease_clock,
    )
    holder = local_writer()
    holder.publish()
    assert holder.advance()
    waiter = local_writer()
    waiter.publish()
    assert not waiter.advance()
    peer_elapsed = [0.0]
    sweeper = make_participant(files, lock_file, "write", stale_threshold=1, clock=lambda: peer_elapsed[0])
    sweeper.publish()
    assert not sweeper.advance()
    old_token = waiter.token
    old_path = str(Path(root, "holders", old_token))
    real_unlink = files.unlink
    granted: list[bool] = []

    def unlink_after_resume(path: str) -> None:
        if path == old_path and not granted:
            granted.append(False)
            holder.leave()
            host_elapsed[0] = 11.0
            host_monotonic[0] = 0.1
            granted[0] = waiter.advance()
        real_unlink(path)

    mocker.patch.object(files, "unlink", autospec=True, side_effect=unlink_after_resume)
    peer_elapsed[0] = 11.0
    assert not sweeper.advance()
    assert granted == [True]
    assert waiter.token != old_token
    assert Path(root, "holders", waiter.token).exists()
    assert waiter.heartbeat() == ("ok", None)


@pytest.fixture
def suspended_host(mocker: MockerFixture) -> tuple[list[float], list[float]]:  # pragma: needs suspend-clock
    host_elapsed = [0.0]
    host_monotonic = [0.0]
    boottime = getattr(time, "CLOCK_MONOTONIC" if sys.platform == "darwin" else "CLOCK_BOOTTIME", None)
    assert boottime is not None

    def boot_clock(clock_id: int) -> float:
        assert clock_id == boottime
        return host_elapsed[0]

    mocker.patch.object(
        heartbeat_mod, "time", new=SimpleNamespace(clock_gettime=boot_clock, monotonic=lambda: host_monotonic[0])
    )
    return host_elapsed, host_monotonic


@pytest.mark.requires_hard_links
def test_commit_after_a_stale_pause_restarts_the_claim(
    lock_file: str, files: OsFiles, root: str, mocker: MockerFixture
) -> None:
    clock = [0.0]
    reader = make_participant(files, lock_file, "read", stale_threshold=10, clock=lambda: clock[0])
    reader.publish()
    judged = reader.token
    real_commit = GenerationLog.commit

    def commit_after_pause(log: GenerationLog, successor: Snapshot) -> bool:
        clock[0] = 10.0
        return real_commit(log, successor)

    mocker.patch.object(GenerationLog, "commit", autospec=True, side_effect=commit_after_pause)
    assert reader.advance()
    assert reader.token != judged
    assert not Path(root, "holders", judged).exists()
    assert reader.generation == 3
    assert GenerationLog(files, lock_file, root).latest().readers == frozenset({reader.token})


@pytest.mark.requires_hard_links
def test_waiting_contender_keeps_an_undeletable_record(
    lock_file: str, files: OsFiles, root: str, mocker: MockerFixture
) -> None:
    # Windows may refuse to rewrite an existing record.
    reader = make_participant(files, lock_file, "read")
    reader.publish()
    mocker.patch.object(OsFiles, "overwrite", return_value=False, autospec=True)
    assert reader.advance()
    assert Path(root, "holders", reader.token).exists()


@pytest.mark.requires_hard_links
def test_blocked_contender_still_evicts_stale_readers(lock_file: str, files: OsFiles, root: str) -> None:
    clock = [0.0]
    dead = make_participant(files, lock_file, "read", stale_threshold=1, clock=lambda: clock[0])
    dead.publish()
    assert dead.advance()
    writer = make_participant(files, lock_file, "write", stale_threshold=1, clock=lambda: clock[0])
    writer.publish()
    assert not writer.advance()
    contender = make_participant(files, lock_file, "read", stale_threshold=1, clock=lambda: clock[0])
    contender.publish()
    assert not contender.advance()
    clock[0] = 5.0
    assert writer.heartbeat() == ("ok", None)  # keeps its own nonce fresh, so only the dead reader goes stale
    assert not contender.advance()
    latest = GenerationLog(files, lock_file, root).latest()
    assert (latest.writer, latest.readers) == (writer.token, frozenset())


def test_advance_hands_back_the_poll_after_losing_every_race(
    lock_file: str, files: OsFiles, mocker: MockerFixture
) -> None:
    writer = make_participant(files, lock_file, "write")
    writer.publish()
    commit = mocker.patch.object(GenerationLog, "commit", autospec=True, return_value=False)
    assert not writer.advance()
    assert commit.call_count == 4
    assert writer.generation is None


@pytest.mark.requires_hard_links
def test_writer_admission_after_the_last_reader_leaves_is_a_commit(lock_file: str, files: OsFiles, root: str) -> None:
    # A writer must publish its grant before a peer can evict it.
    reader = make_participant(files, lock_file, "read")
    reader.publish()
    assert reader.advance()
    writer = make_participant(files, lock_file, "write")
    writer.publish()
    assert not writer.advance()
    reader.leave()
    left_at = GenerationLog(files, lock_file, root).latest().generation
    assert writer.advance()
    assert writer.generation == left_at + 1


@pytest.mark.requires_hard_links
def test_a_commit_reported_lost_that_landed_is_recognized(
    lock_file: str, files: OsFiles, root: str, mocker: MockerFixture
) -> None:
    # A link can succeed before its identity check fails.
    writer = make_participant(files, lock_file, "write")
    writer.publish()
    real_link = OsFiles.link
    denied = []

    def link_then_deny(self: OsFiles, source: str, target: str) -> bool:
        landed = real_link(self, source, target)
        if landed and not denied and Path(target).name.isdigit():
            denied.append(target)
            return False
        return landed

    mocker.patch.object(OsFiles, "link", autospec=True, side_effect=link_then_deny)
    assert writer.advance()
    assert writer.generation == 2
    assert len(denied) == 1
    assert GenerationLog(files, lock_file, root).latest().writer == writer.token


@pytest.mark.requires_hard_links
def test_leave_drops_the_record_when_the_log_cannot_be_read(
    lock_file: str, files: OsFiles, root: str, mocker: MockerFixture
) -> None:
    reader = make_participant(files, lock_file, "read")
    reader.publish()
    mocker.patch.object(GenerationLog, "latest", autospec=True, side_effect=OSError(EIO, "Input/output error"))
    with pytest.raises(OSError, match="Input/output error"):
        reader.leave()
    assert not Path(root, "holders", reader.token).exists()
