from __future__ import annotations

import itertools
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from decimal import Decimal
from threading import Event, Thread
from typing import TYPE_CHECKING, Final, Literal, NoReturn, cast

import pytest

import filelock._soft._lease as lease_mod
from filelock import (
    AsyncSoftFileLease,
    LeaseCompromise,
    LeaseSettingsMismatch,
    SoftFileLease,
    Timeout,
)
from filelock._identity import host_name
from tests.capability_marks import (
    NEEDS_FORK,
)
from tests.fork_helpers import exit_child, fork_process

if TYPE_CHECKING:
    from pathlib import Path
    from types import FrameType

    from _typeshed import Unused  # simplint: ignore[private-import-path]  # precise callback type
    from pytest_mock import MockerFixture

from tests.soft.lease_helpers import DEADLINE, DURATION, HEARTBEAT, LeaseSettings, Reports, make_lease


def test_lease_publishes_its_claim(marker: Path) -> None:
    lease = make_lease(marker)

    with lease:
        owner = lease.owner
        assert owner is not None
        assert (owner.pid, owner.hostname, owner.mode, owner.lease_duration) == (
            os.getpid(),
            host_name(),
            "lease",
            DURATION,
        )
        assert owner.token == lease.token


def test_lease_token_names_the_claim_only_while_held(marker: Path) -> None:
    lease = make_lease(marker)

    with lease:
        held = lease.token

    assert held is not None
    assert lease.token is None


def test_lease_token_names_no_claim_after_a_contended_acquire(marker: Path) -> None:
    contender = make_lease(marker, timeout=0)

    with make_lease(marker):
        with pytest.raises(Timeout):
            contender.acquire()
        assert contender.token is None


_FORK_WARNING: Final[pytest.MarkDecorator] = pytest.mark.filterwarnings(
    "ignore:.*multi-threaded, use of fork.*:DeprecationWarning"
)


@NEEDS_FORK  # pragma: needs fork
@_FORK_WARNING
@pytest.mark.parametrize("thread_local", [pytest.param(True, id="thread-local"), pytest.param(False, id="shared")])
def test_lease_claim_does_not_survive_fork(marker: Path, *, thread_local: bool) -> None:
    # fork copies the parent's claim, token, compromise and heartbeat included, into a child that holds nothing.
    lease = SoftFileLease(
        str(marker),
        lease_duration=DURATION,
        heartbeat_interval=HEARTBEAT,
        thread_local=thread_local,
        is_singleton=False,
    )
    lease.acquire()
    assert lease.token is not None

    def child() -> NoReturn:
        exit_child(0 if (lease.token is None and not lease.is_locked and lease.compromise is None) else 1)

    _, status = os.waitpid(fork_process(child), 0)

    assert os.waitstatus_to_exitcode(status) == 0
    assert lease.token is not None
    lease.release()


def test_lease_token_names_no_claim_after_an_acquire_that_raises(tmp_path: Path) -> None:
    # The one path no rollback covers: a raise before the acquire holds a descriptor.
    (blocker := tmp_path / "blocker").touch()
    lease = make_lease(blocker / "resource.lock")

    with pytest.raises(FileExistsError):
        lease.acquire()

    assert lease.token is None


def test_lease_refuses_to_nest_into_a_lost_claim(marker: Path, mocker: MockerFixture) -> None:
    mocker.patch("filelock._soft._lease.touch", side_effect=OSError("cannot touch the marker"), autospec=True)
    reports = Reports()
    lease = make_lease(marker, on_compromise=reports)

    with lease:
        lost = lease.token
        assert reports.landed.wait(DEADLINE)
        with pytest.raises(RuntimeError, match="current claim was lost"):
            lease.acquire()
        assert lease.lock_counter == 1

    with lease:
        assert lease.token not in {None, lost}


def test_shared_lease_refuses_another_thread_after_loss(marker: Path, mocker: MockerFixture) -> None:
    mocker.patch("filelock._soft._lease.touch", side_effect=OSError("cannot touch the marker"), autospec=True)
    reports = Reports()
    lease = SoftFileLease(
        str(marker), thread_local=False, lease_duration=DURATION, heartbeat_interval=HEARTBEAT, on_compromise=reports
    )

    with lease:
        assert reports.landed.wait(DEADLINE)
        with (
            ThreadPoolExecutor(max_workers=1) as other_thread,
            pytest.raises(RuntimeError, match="current claim was lost"),
        ):
            other_thread.submit(lease.acquire).result(DEADLINE)
        assert lease.lock_counter == 1


def test_lease_accepts_reentry_after_an_old_heartbeat_reports_late(marker: Path, mocker: MockerFixture) -> None:
    blocked, resume, refreshed = Event(), Event(), Event()
    real_touch = lease_mod.touch
    calls = itertools.count()

    def touch(path: str, *, fd: int) -> None:
        if next(calls):
            real_touch(path, fd=fd)
            refreshed.set()
            return
        blocked.set()
        resume.wait(DEADLINE)
        msg = "cannot touch the marker"
        raise OSError(msg)

    mocker.patch("filelock._soft._lease.touch", side_effect=touch, autospec=True)
    reports = Reports()
    lease = SoftFileLease(
        str(marker), lease_duration=HEARTBEAT * 1.5, heartbeat_interval=HEARTBEAT, on_compromise=reports
    )
    lease.acquire()
    old_token = lease.token
    assert blocked.wait(DEADLINE)
    lease.release()

    with lease:
        resume.set()
        assert reports.landed.wait(DEADLINE)
        assert refreshed.wait(DEADLINE)
        with lease:
            assert lease.lock_counter == 2
        assert [compromise.token for compromise in reports.seen] == [old_token]


def test_lease_records_a_float_subclass_duration_as_a_plain_float(marker: Path) -> None:
    with SoftFileLease(str(marker), lease_duration=_Seconds(DURATION), heartbeat_interval=HEARTBEAT):
        assert f"duration={DURATION!r}\n" in marker.read_text(encoding="ascii")


class _Seconds(float):
    # NumPy 2's float64 spells its repr this way; a marker that records it is one no parser reads.
    def __repr__(self) -> str:  # pragma: no cover  # only a lease that records the subclass itself calls it
        return f"_Seconds({float(self)!r})"


def test_lease_singleton_accepts_the_same_decimal_heartbeat_again(marker: Path) -> None:
    # 4.0.12 took any real number; the annotation stays float.
    heartbeat: Final[float] = cast("float", Decimal("0.1"))
    lease: Final[SoftFileLease] = SoftFileLease(
        str(marker), is_singleton=True, lease_duration=DURATION, heartbeat_interval=heartbeat
    )
    assert SoftFileLease(str(marker), is_singleton=True, lease_duration=DURATION, heartbeat_interval=heartbeat) is lease


def test_lease_keeps_its_claim_when_another_thread_fails_to_acquire(marker: Path) -> None:
    # Clearing the holder's heartbeat would let a peer take its live marker.
    holder = make_lease(marker)

    def contend() -> None:
        with suppress(Timeout):
            holder.acquire()

    with holder:
        token = holder.token
        contender = Thread(target=contend)
        contender.start()
        contender.join()

        assert holder.token == token
        time.sleep(  # simplint: ignore[sleep-in-test]  # needs a thread-exit signal
            DURATION * 1.5
        )  # only a surviving heartbeat keeps the claim past this
        with pytest.raises(Timeout):
            make_lease(marker).acquire()


def test_lease_shared_across_threads_has_one_claim(marker: Path) -> None:
    lease: Final[SoftFileLease] = SoftFileLease(
        str(marker), thread_local=False, lease_duration=DURATION, heartbeat_interval=HEARTBEAT
    )
    inside, resume = Event(), Event()

    # Interpreters run a profile hook with tracing off, so coverage never records its body.
    def pause_in_claim_creation(frame: FrameType, event: str, _arg: Unused) -> None:  # pragma: no cover
        if event == "call" and type(frame.f_locals.get("self")).__name__ == "_LeaseClaim":
            inside.set()
            resume.wait()

    def read_compromise() -> None:
        sys.setprofile(pause_in_claim_creation)
        try:
            assert lease.compromise is None
        finally:
            sys.setprofile(None)

    reader: Final[Thread] = Thread(target=read_compromise)
    reader.start()
    # A claim built on first use would park the reader here, and acquire would then build a second one.
    inside.wait(0.5)
    lease.acquire()
    resume.set()
    reader.join()
    try:
        assert lease.token is not None
    finally:
        lease.release()


def test_lease_force_released_for_an_exited_thread_stops_that_holds_heartbeat(marker: Path) -> None:
    # A heartbeat left running would find its marker gone and report a compromise nobody suffered.
    reports: Final[list[LeaseCompromise]] = []
    lease: Final[SoftFileLease] = make_lease(marker, on_compromise=reports.append)
    holder: Final[Thread] = Thread(target=lease.acquire)
    holder.start()
    holder.join()

    lease.release(force=True)
    time.sleep(HEARTBEAT * 3)  # simplint: ignore[sleep-in-test]  # needs a thread-exit signal
    assert (reports, marker.exists()) == ([], False)


def test_lease_hands_back_the_claim_when_the_heartbeat_cannot_start(marker: Path, mocker: MockerFixture) -> None:
    # A failed thread start leaves no heartbeat to renew the marker.
    mocker.patch.object(Thread, "start", side_effect=RuntimeError("can't start new thread"), autospec=True)
    lease = make_lease(marker)
    with pytest.raises(RuntimeError, match="can't start new thread"):
        lease.acquire()

    assert not lease.is_locked
    assert not marker.exists()
    lease.release(force=True)  # nothing was left to release, so this must not raise


def test_lease_release_stops_a_heartbeat_recorded_before_its_thread_starts(marker: Path, mocker: MockerFixture) -> None:
    # A shared context permits release between heartbeat allocation and thread start.
    mocker.patch.object(Thread, "start", autospec=True)
    lease = SoftFileLease(
        str(marker), timeout=0.3, lease_duration=DURATION, heartbeat_interval=HEARTBEAT, thread_local=False
    )
    lease.acquire()
    assert lease.is_locked

    lease.release()  # must not raise joining a thread that never started

    assert not lease.is_locked
    assert not marker.exists()


def test_lease_rejects_a_peer_configured_with_another_duration(marker: Path) -> None:

    with make_lease(marker), pytest.raises(LeaseSettingsMismatch, match="must agree on lease_duration"):
        make_lease(marker, lease_duration=DURATION * 3).acquire()


_LEASE_TYPES: Final[list[pytest.ParameterSet]] = [
    pytest.param(SoftFileLease, id="sync"),
    pytest.param(AsyncSoftFileLease, id="async"),
]


@pytest.mark.parametrize("lease_type", _LEASE_TYPES)
def test_lease_singleton_reuses_the_instance_for_the_same_settings(
    marker: Path, lease_type: type[SoftFileLease], lease_settings: LeaseSettings
) -> None:
    lease = lease_type(str(marker), is_singleton=True, **lease_settings)
    assert lease_type(str(marker), is_singleton=True, **lease_settings) is lease


@pytest.mark.parametrize("lease_type", _LEASE_TYPES)
def test_lease_singleton_resolves_an_omitted_heartbeat_before_comparing(
    marker: Path, lease_type: type[SoftFileLease]
) -> None:
    lease = lease_type(str(marker), is_singleton=True, lease_duration=6)
    assert lease_type(str(marker), is_singleton=True, lease_duration=6, heartbeat_interval=2) is lease


@pytest.mark.parametrize(
    "setting", [pytest.param(name, id=name) for name in ("lease_duration", "heartbeat_interval", "on_compromise")]
)
@pytest.mark.parametrize("lease_type", _LEASE_TYPES)
def test_lease_singleton_rejects_reuse_with_another_setting(
    marker: Path,
    lease_type: type[SoftFileLease],
    lease_settings: LeaseSettings,
    setting: Literal["lease_duration", "heartbeat_interval", "on_compromise"],
) -> None:
    _held = lease_type(str(marker), is_singleton=True, **lease_settings)  # the cache only keeps it weakly
    del lease_settings[setting]
    with pytest.raises(ValueError, match=rf"\n\t{setting} \(existing lock has "):
        lease_type(str(marker), is_singleton=True, **lease_settings)
