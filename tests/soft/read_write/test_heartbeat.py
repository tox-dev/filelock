from __future__ import annotations

import os
import signal
import sys
import threading
import time
from errno import EIO
from multiprocessing import Event, Process
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Final, Literal

import pytest
from capabilities import CAPABILITIES

from filelock import LeaseCompromise, SoftReadWriteLock, Timeout
from filelock._soft._read_write._generations import GenerationLog, Snapshot
from filelock._soft._read_write._heartbeat import HeartbeatThread, lease_clock
from filelock._soft._read_write._participant import Participant
from filelock._soft._read_write._storage import OsFiles
from tests.capability_marks import (
    NEEDS_POSIX_SIGNALS,
    SKIP_ON_UNRELIABLE_PROCESS_SYNC,
)
from tests.process_helpers import cleanup_processes

from .lock_helpers import PROCESS_DEADLINE, holders, make_lock, plant_holder, read_state, wait_until, worker

if TYPE_CHECKING:
    from collections.abc import Callable
    from multiprocessing.synchronize import Event as EventType

    from pytest_mock import MockerFixture

pytestmark: Final[list[pytest.MarkDecorator]] = [
    pytest.mark.requires_hard_links,
    pytest.mark.usefixtures("clear_singletons"),
]


@SKIP_ON_UNRELIABLE_PROCESS_SYNC
@NEEDS_POSIX_SIGNALS
@pytest.mark.timeout(PROCESS_DEADLINE * 3)
@pytest.mark.parametrize(
    ("dead_mode", "contender_mode"),
    [pytest.param("write", "read", id="dead-writer"), pytest.param("read", "write", id="dead-reader")],
)
def test_dead_holder_evicted_by_contender(
    lock_file: str,
    lock: SoftReadWriteLock,
    dead_mode: Literal["read", "write"],
    contender_mode: Literal["read", "write"],
) -> None:  # pragma: needs posix-signals
    held = Event()
    holder = Process(target=_sigkill_worker, args=(lock_file, dead_mode, held, 0.1, 0.5))
    with cleanup_processes([holder]):
        holder.start()
        assert held.wait(timeout=PROCESS_DEADLINE)
        pid = holder.pid
        assert pid is not None
        os.kill(pid, getattr(signal, "SIGKILL"))  # ruff:ignore[get-attr-with-constant] - signal.SIGKILL is POSIX-only
        holder.join(timeout=PROCESS_DEADLINE)
        with getattr(lock, f"{contender_mode}_lock")(timeout=5):
            hold = lock._hold
            assert hold is not None
            assert read_state(lock_file).members == {hold.participant.token}


def _sigkill_worker(  # pragma: forked child
    lock_file: str,
    mode: Literal["read", "write"],
    acquired_event: EventType,
    heartbeat_interval: float,
    stale_threshold: float,
) -> None:
    lock = SoftReadWriteLock(
        lock_file,
        is_singleton=False,
        heartbeat_interval=heartbeat_interval,
        stale_threshold=stale_threshold,
        poll_interval=0.05,
    )
    if mode == "read":
        lock.acquire_read()
    else:
        lock.acquire_write()
    acquired_event.set()
    threading.Event().wait()


def test_heartbeat_reports_eviction_and_stops(lock_file: str) -> None:
    seen: list[LeaseCompromise] = []
    lock = make_lock(lock_file, heartbeat_interval=0.05, stale_threshold=PROCESS_DEADLINE, on_compromise=seen.append)
    lock.acquire_write(timeout=2)
    try:
        hold = lock._hold
        assert hold is not None
        log = GenerationLog(OsFiles(lock_file), lock_file, f"{lock_file}.rw")
        latest = log.latest()
        assert log.commit(Snapshot(generation=latest.generation + 1, writer=None, readers=frozenset()))
        assert hold.heartbeat_stop.wait(timeout=PROCESS_DEADLINE)
        assert lock.compromise == LeaseCompromise(lock_file=lock_file, token=hold.participant.token, reason="evicted")
        assert seen == [lock.compromise]
    finally:
        lock.release(force=True)
        lock.close()
    peer = make_lock(lock_file, heartbeat_interval=0.05, stale_threshold=PROCESS_DEADLINE)
    try:
        with peer.write_lock(timeout=1):
            pass
    finally:
        peer.close()


@pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")
def test_a_raising_on_compromise_still_stops_the_heartbeat(lock_file: str) -> None:
    def fail(_compromise: LeaseCompromise) -> None:
        msg = "callback failed"
        raise RuntimeError(msg)

    lock = make_lock(lock_file, heartbeat_interval=0.05, stale_threshold=PROCESS_DEADLINE, on_compromise=fail)
    lock.acquire_write(timeout=2)
    try:
        hold = lock._hold
        assert hold is not None
        log = GenerationLog(OsFiles(lock_file), lock_file, f"{lock_file}.rw")
        assert log.commit(Snapshot(generation=log.latest().generation + 1, writer=None, readers=frozenset()))
        hold.heartbeat_thread.join(timeout=PROCESS_DEADLINE)
        assert not hold.heartbeat_thread.is_alive()
        assert hold.heartbeat_stop.is_set()
    finally:
        lock.release(force=True)
        lock.close()


def test_heartbeat_reports_a_removed_holder_record(lock_file: str) -> None:
    lock = make_lock(lock_file, heartbeat_interval=0.05, stale_threshold=PROCESS_DEADLINE)
    lock.acquire_read(timeout=2)
    try:
        hold = lock._hold
        assert hold is not None
        Path(f"{lock_file}.rw", "holders", hold.participant.token).unlink()
        assert hold.heartbeat_stop.wait(timeout=PROCESS_DEADLINE)
        assert lock.compromise is not None
        assert lock.compromise.reason == "evicted"
    finally:
        lock.release(force=True)
        lock.close()


@pytest.mark.skipif(
    not (CAPABILITIES["symlink"] and CAPABILITIES["link-dir-fd"]),
    reason="a swapped tree is refused only where records are reached through descriptors",
)
def test_heartbeat_reports_a_symlinked_tree_as_lost(
    lock_file: str, tmp_path: Path
) -> None:  # pragma: needs link-dir-fd
    seen: list[LeaseCompromise] = []
    lock = make_lock(lock_file, heartbeat_interval=0.05, stale_threshold=PROCESS_DEADLINE, on_compromise=seen.append)
    lock.acquire_write(timeout=2)
    try:
        Path(f"{lock_file}.rw").rename(tmp_path / "aside")
        Path(f"{lock_file}.rw").symlink_to(tmp_path, target_is_directory=True)
        assert _wait_for_compromise(lock).reason == "evicted"
        assert len(seen) == 1
    finally:
        lock.release(force=True)
        lock.close()


def _wait_for_compromise(lock: SoftReadWriteLock) -> LeaseCompromise:
    wait_until(lambda: lock.compromise is not None)
    compromise = lock.compromise
    assert compromise is not None
    return compromise


@pytest.mark.skipif(sys.platform != "win32", reason="only Windows reaches records by name, through junctions")
def test_heartbeat_reports_a_junction_tree_as_lost(  # pragma: win32 cover
    lock_file: str, tmp_path: Path, create_junction: Callable[[Path, Path], None]
) -> None:
    seen: list[LeaseCompromise] = []
    lock = make_lock(lock_file, heartbeat_interval=0.05, stale_threshold=PROCESS_DEADLINE, on_compromise=seen.append)
    lock.acquire_write(timeout=2)
    try:
        Path(f"{lock_file}.rw").rename(tmp_path / "aside")
        (target := tmp_path / "target").mkdir()
        create_junction(Path(f"{lock_file}.rw"), target)
        assert _wait_for_compromise(lock).reason == "evicted"
        assert len(seen) == 1
    finally:
        lock.release(force=True)
        lock.close()


def test_a_heartbeat_that_raises_reports_the_hold_lost(lock_file: str, mocker: MockerFixture) -> None:
    error = ValueError("broken")
    lock = make_lock(lock_file, heartbeat_interval=0.05, stale_threshold=PROCESS_DEADLINE)
    lock.acquire_write(timeout=2)
    try:
        mocker.patch.object(Participant, "heartbeat", autospec=True, side_effect=error)
        compromise = _wait_for_compromise(lock)
        assert (compromise.reason, compromise.error) == ("refresh-failed", error)
    finally:
        mocker.stopall()
        lock.release(force=True)
        lock.close()


def test_release_leaves_a_peers_claim_alone(lock_file: str) -> None:
    lock = make_lock(lock_file, heartbeat_interval=10, stale_threshold=40)
    lock.acquire_write(timeout=2)
    try:
        peer = plant_holder(lock_file, mode="write")
        lock.release()
        assert read_state(lock_file).writer == peer
        assert holders(lock_file) == [peer]
    finally:
        lock.close()


def test_heartbeat_survives_a_transient_refresh_error(lock_file: str, mocker: MockerFixture) -> None:
    # A transient filesystem error can clear before the holder lease expires.
    lock = make_lock(lock_file, heartbeat_interval=0.02, stale_threshold=5)
    lock.acquire_write(timeout=2)
    try:
        hold = lock._hold
        assert hold is not None
        overwrite = mocker.patch.object(
            OsFiles, "overwrite", side_effect=OSError(EIO, "Input/output error"), autospec=True
        )
        wait_until(lambda: overwrite.call_count >= 10 or hold.heartbeat_stop.is_set())
        assert hold.heartbeat_thread.is_alive()
        assert not hold.heartbeat_stop.is_set()
        assert lock.compromise is None
    finally:
        mocker.stopall()
        lock.release(force=True)
        lock.close()


def test_heartbeat_reports_refresh_failures_that_outlast_the_margin(lock_file: str, mocker: MockerFixture) -> None:
    seen: list[LeaseCompromise] = []
    lock = make_lock(lock_file, heartbeat_interval=0.02, stale_threshold=PROCESS_DEADLINE, on_compromise=seen.append)
    lock.acquire_write(timeout=2)
    try:
        hold = lock._hold
        assert hold is not None
        mocker.patch.object(OsFiles, "overwrite", side_effect=OSError(EIO, "Input/output error"), autospec=True)
        mocker.patch(
            "filelock._soft._read_write._hold.lease_clock",
            autospec=True,
            side_effect=lambda: hold.last_refresh + lock.stale_threshold,
        )
        assert hold.heartbeat_stop.wait(timeout=PROCESS_DEADLINE)
        assert [compromise.reason for compromise in seen] == ["refresh-failed"]
    finally:
        mocker.stopall()
        lock.release(force=True)
        lock.close()


@pytest.mark.skipif(not CAPABILITIES["suspend-clock"], reason="needs a clock that counts host suspension")
def test_heartbeat_reports_refresh_failure_after_host_suspend(
    lock_file: str, mocker: MockerFixture
) -> None:  # pragma: needs suspend-clock
    seen: list[LeaseCompromise] = []
    lock = make_lock(lock_file, heartbeat_interval=10, stale_threshold=30, on_compromise=seen.append)
    lock.acquire_write(timeout=2)
    try:
        hold = lock._hold
        assert hold is not None
        mocker.patch.object(
            Participant, "heartbeat", autospec=True, return_value=("transient", OSError(EIO, "stale filesystem"))
        )
        mocker.patch(
            "filelock._soft._read_write._heartbeat.time.clock_gettime",
            autospec=True,
            return_value=lease_clock() + 31,
        )
        assert not hold.refresh(lock, lock._report_compromise)
        assert [compromise.reason for compromise in seen] == ["refresh-failed"]
    finally:
        mocker.stopall()
        lock.release(force=True)
        lock.close()


@pytest.mark.parametrize("mode", [pytest.param("write", id="write"), pytest.param("read", id="read")])
def test_acquire_hands_back_the_slot_when_the_heartbeat_cannot_start(
    lock_file: str, lock: SoftReadWriteLock, mocker: MockerFixture, mode: Literal["read", "write"]
) -> None:
    mocker.patch.object(HeartbeatThread, "start", side_effect=RuntimeError("can't start new thread"), autospec=True)
    acquire = getattr(lock, f"acquire_{mode}")
    with pytest.raises(RuntimeError, match="can't start new thread"):
        acquire(timeout=2)

    assert lock._hold is None
    assert read_state(lock_file).members == frozenset()
    assert holders(lock_file) == []
    lock.release(force=True)  # a handed-back slot leaves nothing to release, so this must not raise


def test_participant_reads_the_clock_patched_after_construction(lock_file: str, mocker: MockerFixture) -> None:
    lock = make_lock(lock_file)
    clock = mocker.create_autospec(time.monotonic, side_effect=time.monotonic)
    mocker.patch(
        "filelock._soft._read_write._lock.time",
        new=SimpleNamespace(monotonic=clock, perf_counter=time.perf_counter, sleep=time.sleep),
    )
    try:
        lock.acquire_write(timeout=2)
        assert lock._hold is not None
        assert lock._hold.participant._clock is clock
    finally:
        mocker.stopall()
        lock.release(force=True)
        lock.close()


@pytest.mark.parametrize("mode", [pytest.param("read", id="read"), pytest.param("write", id="write")])
@pytest.mark.parametrize("force", [pytest.param(True, id="force"), pytest.param(False, id="plain")])
def test_release_from_on_compromise_leaves_cleanly(lock_file: str, force: bool, mode: Literal["read", "write"]) -> None:
    # A compromise callback runs on the heartbeat thread and cannot join itself.
    lock = SoftReadWriteLock(
        lock_file,
        is_singleton=False,
        heartbeat_interval=0.05,
        stale_threshold=PROCESS_DEADLINE,
        poll_interval=0.02,
        on_compromise=lambda _compromise: lock.release(force=force),
    )
    getattr(lock, f"acquire_{mode}")(timeout=2)
    hold = lock._hold
    assert hold is not None
    try:
        log = GenerationLog(OsFiles(lock_file), lock_file, f"{lock_file}.rw")
        latest = log.latest()
        assert log.commit(Snapshot(generation=latest.generation + 1, writer=None, readers=frozenset()))
        assert hold.heartbeat_stop.wait(timeout=PROCESS_DEADLINE)
        hold.heartbeat_thread.join(timeout=PROCESS_DEADLINE)
        assert lock._hold is None
        assert holders(lock_file) == []
    finally:
        lock.close()


def test_late_heartbeat_tick_does_not_stamp_a_later_hold(lock_file: str) -> None:
    lock = make_lock(lock_file, heartbeat_interval=10, stale_threshold=40)
    lock.acquire_write(timeout=2)
    old = lock._hold
    assert old is not None
    lock.release()
    lock.acquire_write(timeout=2)
    try:
        lock._report_compromise(old, "evicted", None)
        assert lock.compromise is None
    finally:
        lock.release()
        lock.close()


@SKIP_ON_UNRELIABLE_PROCESS_SYNC
@pytest.mark.timeout(PROCESS_DEADLINE * 3)
def test_live_heartbeat_keeps_lock_alive_past_stale_threshold(lock_file: str) -> None:
    # A Windows worker may take several seconds to start.
    heartbeat, stale = 0.3, 1.5
    held, release = Event(), Event()
    holder = Process(
        target=worker,
        args=(lock_file, "write", held, release, -1, True, heartbeat, stale, 0.05),
    )
    with cleanup_processes([holder]):
        holder.start()
        assert held.wait(timeout=PROCESS_DEADLINE)
        time.sleep(stale * 2)  # simplint: ignore[sleep-in-test]  # needs a clock hook in soft rw
        lock = make_lock(lock_file, heartbeat_interval=heartbeat, stale_threshold=stale)
        try:
            with pytest.raises(Timeout):
                lock.acquire_write(timeout=stale * 2)
        finally:
            lock.close()
        release.set()
        holder.join(timeout=PROCESS_DEADLINE)
