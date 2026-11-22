from __future__ import annotations

import itertools
import os
import socket
import threading
import time
from threading import Event, Thread
from types import SimpleNamespace
from typing import TYPE_CHECKING, Final, cast

import pytest

import filelock._soft._lease as lease_mod
from filelock import (
    FileLock,
    SoftFileLease,
    StrictSoftFileLock,
    Timeout,
)
from filelock._identity import host_name
from tests.capability_marks import (
    NEEDS_STALE_RECLAIM,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from pytest_mock import MockerFixture

from tests.soft.lease_helpers import DURATION, HEARTBEAT, make_lease


@pytest.mark.requires_hard_links
def test_lease_does_not_expire_a_strict_holder(marker: Path) -> None:  # pragma: needs hard-link
    # A strict holder never agreed to be superseded by age, so a lease contender waits it out instead.
    with StrictSoftFileLock(str(marker)):
        with pytest.raises(Timeout):
            make_lease(marker, timeout=DURATION * 2).acquire()
        assert marker.exists()


@pytest.mark.parametrize(
    ("lease_duration", "heartbeat_interval", "error", "message"),
    [
        pytest.param(0, None, ValueError, "lease_duration must be positive", id="zero-duration"),
        pytest.param(-1, None, ValueError, "lease_duration must be positive", id="negative-duration"),
        pytest.param(float("nan"), HEARTBEAT, ValueError, "positive and finite", id="nan-duration"),
        pytest.param(float("inf"), HEARTBEAT, ValueError, "positive and finite", id="infinite-duration"),
        pytest.param(float("-inf"), HEARTBEAT, ValueError, "positive and finite", id="negative-infinite-duration"),
        pytest.param(True, None, TypeError, "number of seconds, not bool", id="true-duration"),
        pytest.param(False, None, TypeError, "number of seconds, not bool", id="false-duration"),
        pytest.param(DURATION, 0, ValueError, "heartbeat_interval must be positive", id="zero-heartbeat"),
        pytest.param(DURATION, DURATION, ValueError, "below lease_duration", id="heartbeat-equals-duration"),
        pytest.param(DURATION, DURATION * 2, ValueError, "below lease_duration", id="heartbeat-over-duration"),
        pytest.param(
            threading.TIMEOUT_MAX * 4,
            threading.TIMEOUT_MAX * 2,
            ValueError,
            "TIMEOUT_MAX",
            id="heartbeat-over-timeout-max",
        ),
        pytest.param(
            threading.TIMEOUT_MAX * 4, None, ValueError, "TIMEOUT_MAX", id="default-heartbeat-over-timeout-max"
        ),
    ],
)
def test_lease_rejects_incoherent_settings(
    marker: Path,
    lease_duration: float,
    heartbeat_interval: float | None,
    error: type[Exception],
    message: str,
) -> None:
    with pytest.raises(error, match=message):
        SoftFileLease(str(marker), lease_duration=lease_duration, heartbeat_interval=heartbeat_interval)


def test_lease_defaults_the_heartbeat_below_the_duration(marker: Path) -> None:
    lease = SoftFileLease(str(marker), lease_duration=30)

    assert lease.lease_duration == 30


def test_lease_drops_lifetime_with_a_warning(marker: Path) -> None:
    with pytest.warns(UserWarning, match="lease_duration sets when a lease expires"):
        lease = SoftFileLease(str(marker), lease_duration=DURATION, lifetime=5)

    assert lease.lifetime is None


@NEEDS_STALE_RECLAIM
def test_lease_supersedes_a_live_holder_once_its_claim_ages_out(marker: Path, mocker: MockerFixture) -> None:
    # Windows forbids removing a live holder's open marker.
    mocker.patch(
        "filelock._soft._lease.owner_is_stale",
        new=mocker.create_autospec(lease_mod.owner_is_stale, return_value=False),
    )
    mocker.patch(
        "filelock._soft._exclusive._recovery.time",
        new=SimpleNamespace(
            monotonic=mocker.create_autospec(time.monotonic, side_effect=itertools.count(step=DURATION)),
            sleep=time.sleep,
        ),
    )
    marker.write_text(
        f"filelock/2\npid={os.getpid()}\nhost={host_name()}\nmode=lease\ntoken=stalled\nduration={DURATION!r}\n",
        encoding="utf-8",
    )

    # The stepped clock ages the marker out on the second look; the real timeout only has to outlast two polls.
    with make_lease(marker, timeout=30) as contender:
        assert contender.is_lock_held_by_us
        assert contender.token not in {None, "stalled"}


def test_native_lock_rejects_a_lease_duration(marker: Path) -> None:
    # An inode lock outlives its pathname.
    construct = cast("Callable[..., FileLock]", FileLock)

    with pytest.raises(TypeError, match="does not support non-default lock options: lease_duration"):
        construct(str(marker), lease_duration=5)


def test_lease_does_not_age_out_a_live_exclusive_owner(marker: Path, mocker: MockerFixture) -> None:
    # An exclusive owner never agreed to expire, so only proof of death may reclaim it, however long it sits unchanged.
    mocker.patch(
        "filelock._soft._lease.owner_is_stale",
        new=mocker.create_autospec(lease_mod.owner_is_stale, return_value=False),
    )
    mocker.patch(
        "filelock._soft._exclusive._recovery.time",
        new=SimpleNamespace(
            monotonic=mocker.create_autospec(time.monotonic, side_effect=itertools.count(step=DURATION)),
            sleep=time.sleep,
        ),
    )
    marker.write_text(f"filelock/2\npid={os.getpid()}\nhost={socket.gethostname()}\nmode=exclusive\n", encoding="utf-8")

    with pytest.raises(Timeout):
        make_lease(marker).acquire()

    assert marker.exists()


def test_lease_keeps_a_claim_refreshed_by_a_host_whose_clock_lags(marker: Path, mocker: MockerFixture) -> None:
    # Clock skew makes a refreshed remote marker look old to this host.
    mocker.patch(
        "filelock._soft._lease.owner_is_stale",
        new=mocker.create_autospec(lease_mod.owner_is_stale, return_value=False),
    )
    marker.write_text(
        f"filelock/2\npid={os.getpid()}\nhost={host_name()}\nmode=lease\ntoken=skewed\nduration={DURATION!r}\n",
        encoding="utf-8",
    )
    stop: Final[Event] = Event()

    def refresh_an_hour_behind() -> None:
        while not stop.wait(HEARTBEAT):
            os.utime(marker, (lagging := time.time() - 3600, lagging))

    refresher: Final[Thread] = Thread(target=refresh_an_hour_behind)
    refresher.start()
    try:
        with pytest.raises(Timeout):
            make_lease(marker, timeout=DURATION * 3).acquire()
    finally:
        stop.set()
        refresher.join()

    assert marker.read_text(encoding="utf-8").endswith("token=skewed\nduration=0.9\n")
