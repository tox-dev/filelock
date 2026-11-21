from __future__ import annotations

import gc
import itertools
import os
import subprocess  # ruff:ignore[suspicious-subprocess-import]  # interpreter exit finalizes a held lease
import sys
import threading
from errno import ENOENT
from threading import Event, Thread
from types import SimpleNamespace
from typing import TYPE_CHECKING, Final

import pytest

import filelock._soft._exclusive._publication as soft_mod
from filelock import (
    LeaseCompromise,
    SoftFileLease,
)
from filelock._identity import host_name
from tests.capability_marks import (
    NEEDS_COLLECTED_FINALIZATION,
    NEEDS_PROMPT_FINALIZATION,
    NEEDS_STALE_RECLAIM,
    NEEDS_UNLINK_OPEN_FILE,
)

if TYPE_CHECKING:
    from pathlib import Path

    from pytest_mock import MockerFixture

from tests.soft.lease_helpers import DEADLINE, DURATION, HEARTBEAT, Reports, make_lease, wait_until


@NEEDS_COLLECTED_FINALIZATION
def test_lease_dropped_while_held_releases_its_marker(marker: Path) -> None:
    lease = make_lease(marker)
    lease.acquire()

    del lease
    gc.collect()
    # PyPy may run a pending finalizer after collection returns.
    wait_until(lambda: not marker.exists())

    assert not marker.exists()


@NEEDS_COLLECTED_FINALIZATION
def test_lease_dropped_where_it_cannot_be_released_stops_its_heartbeat(marker: Path) -> None:
    # The last reference can be dropped on a thread that cannot see the owning context.
    before: Final[set[Thread]] = _lease_heartbeats()
    leases: Final[list[SoftFileLease]] = []

    def acquire() -> None:
        lease: Final[SoftFileLease] = make_lease(marker)
        lease.acquire()
        leases.append(lease)

    acquirer: Final[Thread] = Thread(target=acquire)
    acquirer.start()
    acquirer.join()
    heartbeats: Final[set[Thread]] = _lease_heartbeats() - before
    assert heartbeats

    leases.clear()
    # A tracing collector finalizes the lease on the first pass and runs the weak reference callback on the next.
    gc.collect()
    gc.collect()

    for heartbeat in heartbeats:
        heartbeat.join(timeout=DURATION * 20)
    assert not any(heartbeat.is_alive() for heartbeat in heartbeats)


def _lease_heartbeats() -> set[threading.Thread]:
    return {thread for thread in threading.enumerate() if thread.name.startswith("filelock-lease-")}


@NEEDS_PROMPT_FINALIZATION
@pytest.mark.parametrize(
    "script",
    [
        pytest.param(
            "import sys, filelock; lease = filelock.SoftFileLease(sys.argv[1]); lease.acquire()", id="dropped"
        ),
        pytest.param(
            "import sys, filelock\n"
            "class Releaser:\n"
            "    def __init__(self, lease): self.lease = lease\n"
            "    def __del__(self): self.lease.release()\n"
            "releaser = Releaser(filelock.SoftFileLease(sys.argv[1]))\n"
            "releaser.lease.acquire()",
            id="released-by-a-finalizer",
        ),
    ],
)
def test_lease_held_at_interpreter_exit_releases_its_marker(marker: Path, script: str) -> None:
    result: Final[subprocess.CompletedProcess[str]] = subprocess.run(
        [sys.executable, "-c", script, str(marker)], check=True, capture_output=True, text=True, timeout=10
    )

    assert (marker.exists(), result.stderr) == (False, "")


@NEEDS_COLLECTED_FINALIZATION
def test_lease_dropped_while_its_heartbeat_finds_the_claim_lost_reports_nothing(
    marker: Path, mocker: MockerFixture
) -> None:
    # Collection during refresh leaves no holder to receive a compromise notification.
    reports = Reports()
    leases: Final[list[SoftFileLease]] = [
        SoftFileLease(
            str(marker),
            thread_local=False,
            lease_duration=DURATION,
            heartbeat_interval=HEARTBEAT,
            on_compromise=reports,
        )
    ]
    before: Final[set[Thread]] = _lease_heartbeats()
    open_spy = (
        mocker.spy(soft_mod, "create_exclusive_non_reparse_fd")
        if sys.platform == "win32"  # pragma: win32 cover
        else mocker.spy(os, "open")
    )
    leases[0].acquire()
    heartbeats: Final[set[Thread]] = _lease_heartbeats() - before
    lease_fd = open_spy.spy_return
    real_lstat = os.lstat
    real_fstat = os.fstat
    misses: Final[itertools.count[int]] = itertools.count()

    def lstat(path: str | os.PathLike[str]) -> os.stat_result:
        if leases and os.fspath(path).endswith(marker.name):
            # A single miss may be a peer's break in flight, so the loss only counts on the second.
            if next(misses):
                leases.clear()
                gc.collect()
            raise FileNotFoundError(ENOENT, "No such file or directory", marker.name)
        return real_lstat(path)

    def fstat(fd: int) -> os.stat_result | SimpleNamespace:
        # The collected lease has closed its descriptor by the time the heartbeat inspects it.
        return SimpleNamespace(st_nlink=0, st_dev=0, st_ino=0) if fd == lease_fd else real_fstat(fd)

    mocker.patch("filelock._soft._lease.os.lstat", autospec=True, side_effect=lstat)
    mocker.patch("filelock._soft._lease.os.fstat", autospec=True, side_effect=fstat)
    for heartbeat in heartbeats:
        heartbeat.join(timeout=DURATION * 20)

    assert (leases, reports.seen, any(heartbeat.is_alive() for heartbeat in heartbeats)) == ([], [], False)


@pytest.mark.skipif(sys.platform == "win32", reason="Windows cannot exchange two names")
@NEEDS_STALE_RECLAIM
def test_lease_takes_over_a_dead_owners_marker_with_its_own_lease_record(  # pragma: win32 no cover
    tmp_path: Path,
) -> None:
    (lock := tmp_path / "a.lock").write_text(
        f"filelock/2\npid={2**22 + 1}\nhost={host_name()}\nmode=exclusive\n", encoding="utf-8"
    )
    lease: Final[SoftFileLease] = SoftFileLease(lock, lease_duration=5)
    lease.acquire(blocking=False)
    try:
        owner = lease.owner
        assert owner is not None
        assert (owner.mode, owner.token, owner.lease_duration, lease.is_lock_held_by_us) == (
            "lease",
            lease.token,
            5.0,
            True,
        )
    finally:
        lease.release()


@NEEDS_UNLINK_OPEN_FILE  # pragma: needs unlink-open-file
def test_lease_dropped_on_its_heartbeat_thread_is_released_there(marker: Path, mocker: MockerFixture) -> None:
    # A finalizer on the heartbeat thread cannot wait for that same thread to stop.
    mocker.patch("filelock._soft._lease.touch", side_effect=OSError("cannot touch the marker"), autospec=True)
    holder: list[SoftFileLease] = []
    heartbeats: list[Thread] = []

    def drop_the_last_reference(_: LeaseCompromise) -> None:
        heartbeats.append(threading.current_thread())
        holder.clear()

    lease = SoftFileLease(
        str(marker),
        is_singleton=False,
        thread_local=False,
        lease_duration=DURATION,
        heartbeat_interval=HEARTBEAT,
        on_compromise=drop_the_last_reference,
    )
    holder.append(lease)
    del lease
    holder[0].acquire()

    wait_until(lambda: bool(heartbeats))
    heartbeats[0].join(DEADLINE)

    assert not heartbeats[0].is_alive()


def test_lease_finalized_inside_its_heartbeat_wait_is_released(marker: Path, mocker: MockerFixture) -> None:
    # PyPy can finalize the lease while the heartbeat thread holds the event's lock.
    real_wait = Event.wait
    holder: list[SoftFileLease] = []
    heartbeats: list[Thread] = []

    def collect_inside_wait(event: Event, timeout: float | None = None) -> bool:
        if holder and threading.current_thread().name.startswith("filelock-lease"):
            heartbeats.append(threading.current_thread())
            with event._cond:  # ty: ignore[missing-attribute]  # typeshed omits the lock wait() holds
                holder.clear()
                gc.collect()
        return real_wait(event, timeout)

    mocker.patch.object(Event, "wait", autospec=True, side_effect=collect_inside_wait)
    lease = SoftFileLease(
        str(marker), is_singleton=False, thread_local=False, lease_duration=DURATION, heartbeat_interval=HEARTBEAT
    )
    lease.acquire()
    holder.append(lease)
    del lease

    wait_until(lambda: bool(heartbeats))
    heartbeats[0].join(DEADLINE)

    assert not heartbeats[0].is_alive()
