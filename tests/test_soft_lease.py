from __future__ import annotations

import itertools
import os
import socket
import sys
import time
from errno import EIO
from typing import TYPE_CHECKING, Final, cast

import pytest

from filelock import (
    FileLock,
    LeaseCompromise,
    LeaseSettingsMismatch,
    SoftFileLease,
    StrictSoftFileLock,
    Timeout,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from pytest_mock import MockerFixture

#: Short enough to keep the suite quick, long enough that a loaded runner still refreshes twice before expiry.
_DURATION: float = 0.9
_HEARTBEAT: float = 0.1

#: Windows refuses to rename or delete a file another process holds open, so a live holder's marker cannot be taken
#: from it. A lease only reclaims there once the holder exits and its handle closes.
_REQUIRES_TAKING_A_LIVE_HOLDERS_MARKER: Final[pytest.MarkDecorator] = pytest.mark.skipif(
    sys.platform == "win32",
    reason="Windows keeps an open marker undeletable, so no peer can take it from a live holder",
)


@pytest.fixture
def marker(tmp_path: Path) -> Path:
    return tmp_path / "a.lock"


def _lease(
    marker: Path,
    *,
    lease_duration: float = _DURATION,
    timeout: float = 0.3,
    on_compromise: Callable[[LeaseCompromise], None] | None = None,
) -> SoftFileLease:
    return SoftFileLease(
        str(marker),
        timeout=timeout,
        lease_duration=lease_duration,
        heartbeat_interval=_HEARTBEAT,
        on_compromise=on_compromise,
    )


def test_lease_publishes_its_claim(marker: Path) -> None:
    lease = _lease(marker)

    with lease:
        owner = lease.owner
        assert owner is not None
        assert (owner.pid, owner.hostname, owner.mode, owner.lease_duration) == (
            os.getpid(),
            socket.gethostname(),
            "lease",
            _DURATION,
        )
        assert owner.token == lease.token


def test_lease_token_names_the_claim_only_while_held(marker: Path) -> None:
    lease = _lease(marker)

    with lease:
        held = lease.token

    assert held is not None
    assert lease.token is None


def test_lease_heartbeat_keeps_a_live_claim_past_its_duration(marker: Path) -> None:
    holder = _lease(marker)

    with holder:
        time.sleep(_DURATION * 1.5)  # only a refreshing heartbeat keeps the claim past this
        with pytest.raises(Timeout):
            _lease(marker).acquire()


@_REQUIRES_TAKING_A_LIVE_HOLDERS_MARKER
def test_lease_peer_takes_an_expired_claim(marker: Path, mocker: MockerFixture) -> None:
    # A wedged holder: its marker stays on disk, but no refresh ever lands on it again, so the claim ages out.
    mocker.patch("filelock._lease.touch")
    holder = _lease(marker)
    holder.acquire()

    try:
        peer = _lease(marker, timeout=_DURATION * 5)
        with peer:
            assert peer.is_lock_held_by_us
            assert peer.token != holder.token
    finally:
        holder.release()


def test_lease_self_heals_a_malformed_marker(marker: Path) -> None:
    # A partial write or a foreign file leaves a marker the lease parser cannot read. Rather than block every
    # contender until timeout, the base self-heal evicts it once it ages past the malformed grace window.
    marker.write_text("not a protocol 2 record\n", encoding="utf-8")
    os.utime(marker, (0, 0))

    with _lease(marker) as lease:
        assert lease.is_lock_held_by_us


def test_lease_reclaims_a_dead_same_host_holder(marker: Path) -> None:
    marker.write_text(
        f"filelock/2\npid=999999\nhost={socket.gethostname()}\nmode=lease\ntoken=abc\nduration={_DURATION!r}\n",
        encoding="utf-8",
    )

    with _lease(marker) as lease:
        assert lease.is_lock_held_by_us


@_REQUIRES_TAKING_A_LIVE_HOLDERS_MARKER
def test_lease_reports_compromise_when_the_marker_vanishes(marker: Path) -> None:
    seen: list[LeaseCompromise] = []
    lease = _lease(marker, on_compromise=seen.append)

    with lease:
        token = lease.token
        marker.unlink()
        time.sleep(_HEARTBEAT * 5)

    assert [(c.reason, c.token, c.lock_file) for c in seen] == [("marker-missing", token, str(marker))]


@_REQUIRES_TAKING_A_LIVE_HOLDERS_MARKER
def test_lease_reports_compromise_when_a_peer_takes_over(marker: Path) -> None:
    seen: list[LeaseCompromise] = []
    holder = _lease(marker, on_compromise=seen.append)
    peer = _lease(marker)

    try:
        with holder:
            marker.unlink()
            peer.acquire()  # a peer publishes a fresh marker at the same path
            time.sleep(_HEARTBEAT * 5)
        assert [c.reason for c in seen] == ["owner-changed"]
    finally:
        peer.release()  # stop the peer's heartbeat here, so its release log never lands in a later test's caplog


def test_lease_reports_compromise_when_a_refresh_fails(marker: Path, mocker: MockerFixture) -> None:
    seen: list[LeaseCompromise] = []
    failure = OSError("cannot touch the marker")
    mocker.patch("filelock._lease.touch", side_effect=failure)
    lease = _lease(marker, on_compromise=seen.append)

    with lease:
        time.sleep(_DURATION + _HEARTBEAT)  # past the refresh margin, so a persistent failure is reported once

    assert [(c.reason, c.error) for c in seen] == [("refresh-failed", failure)]


@pytest.mark.parametrize("target", [pytest.param("touch", id="touch"), pytest.param("os.lstat", id="lstat")])
def test_lease_tolerates_a_transient_refresh_error(marker: Path, mocker: MockerFixture, target: str) -> None:
    # A transient ESTALE/EIO on the refresh path that recovers before the lease could lapse must not raise a
    # compromise: the marker was ours last tick, so retry rather than tell the holder to abandon its work.
    import filelock._lease as lease_mod

    ticks = itertools.count()
    real = cast("Callable[..., object]", lease_mod.touch if target == "touch" else os.lstat)

    def flaky(path: str, *args: object, **kwargs: object) -> object:
        if path.endswith(marker.name) and next(ticks) < 2:
            raise OSError(EIO, "Input/output error")
        return real(path, *args, **kwargs)

    mocker.patch(f"filelock._lease.{target}", side_effect=flaky)
    seen: list[LeaseCompromise] = []
    lease = _lease(marker, on_compromise=seen.append)
    with lease:
        time.sleep(_HEARTBEAT * 6)  # several ticks: the first two fail, the rest recover
        assert seen == []
        assert lease.compromise is None


@_REQUIRES_TAKING_A_LIVE_HOLDERS_MARKER
def test_lease_reports_one_compromise_per_claim(marker: Path) -> None:
    seen: list[LeaseCompromise] = []
    lease = _lease(marker, on_compromise=seen.append)

    with lease:
        marker.unlink()
        time.sleep(_HEARTBEAT * 6)  # several refreshes fail, but the holder is told once

    assert len(seen) == 1


@_REQUIRES_TAKING_A_LIVE_HOLDERS_MARKER
def test_lease_records_the_compromise_without_a_callback(marker: Path) -> None:
    lease = _lease(marker)

    with lease:
        marker.unlink()
        time.sleep(_HEARTBEAT * 5)
        compromise = lease.compromise

    assert compromise is not None
    assert compromise.reason == "marker-missing"


def test_lease_holds_an_uncompromised_claim(marker: Path) -> None:
    lease = _lease(marker)

    with lease:
        time.sleep(_HEARTBEAT * 3)
        assert lease.compromise is None


@_REQUIRES_TAKING_A_LIVE_HOLDERS_MARKER
def test_lease_can_be_released_from_the_compromise_callback(marker: Path) -> None:
    # The callback runs on the heartbeat thread, so releasing from it needs a context that thread can see, and it must
    # not deadlock joining itself.
    holder: list[SoftFileLease] = []

    def release_the_claim(_: LeaseCompromise) -> None:
        holder[0].release()

    lease = SoftFileLease(
        str(marker),
        thread_local=False,
        lease_duration=_DURATION,
        heartbeat_interval=_HEARTBEAT,
        on_compromise=release_the_claim,
    )
    holder.append(lease)
    lease.acquire()

    marker.unlink()
    time.sleep(_HEARTBEAT * 5)

    assert lease.compromise is not None
    assert not lease.is_locked


@_REQUIRES_TAKING_A_LIVE_HOLDERS_MARKER
def test_lease_release_from_the_callback_needs_a_shared_context(marker: Path) -> None:
    # With the default thread-local context the heartbeat thread sees no claim of its own, so its release() does
    # nothing. Pin the trap the docstring warns about.
    holder: list[SoftFileLease] = []

    def release_the_claim(_: LeaseCompromise) -> None:
        holder[0].release()

    lease = _lease(marker, on_compromise=release_the_claim)
    holder.append(lease)
    lease.acquire()

    try:
        marker.unlink()
        time.sleep(_HEARTBEAT * 5)
        assert lease.is_locked, "a thread-local release() from the heartbeat thread silently did nothing"
    finally:
        lease.release()


def test_lease_rejects_a_peer_configured_with_another_duration(marker: Path) -> None:
    holder = _lease(marker)

    with holder, pytest.raises(LeaseSettingsMismatch, match="must agree on lease_duration"):
        _lease(marker, lease_duration=_DURATION * 3).acquire()


@pytest.mark.requires_hard_links
def test_lease_does_not_expire_a_strict_holder(marker: Path) -> None:
    # A strict holder never agreed to be superseded by age, so a lease contender waits it out instead.
    with StrictSoftFileLock(str(marker)):
        with pytest.raises(Timeout):
            _lease(marker, timeout=_DURATION * 2).acquire()
        assert marker.exists()


@pytest.mark.parametrize(
    ("lease_duration", "heartbeat_interval", "message"),
    [
        pytest.param(0, None, "lease_duration must be positive", id="zero-duration"),
        pytest.param(-1, None, "lease_duration must be positive", id="negative-duration"),
        pytest.param(_DURATION, 0, "heartbeat_interval must be positive", id="zero-heartbeat"),
        pytest.param(_DURATION, _DURATION, "below lease_duration", id="heartbeat-equals-duration"),
        pytest.param(_DURATION, _DURATION * 2, "below lease_duration", id="heartbeat-over-duration"),
    ],
)
def test_lease_rejects_incoherent_settings(
    marker: Path, lease_duration: float, heartbeat_interval: float | None, message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        SoftFileLease(str(marker), lease_duration=lease_duration, heartbeat_interval=heartbeat_interval)


def test_lease_defaults_the_heartbeat_below_the_duration(marker: Path) -> None:
    lease = SoftFileLease(str(marker), lease_duration=30)

    assert lease.lease_duration == 30


def test_lease_drops_lifetime_with_a_warning(marker: Path) -> None:
    with pytest.warns(UserWarning, match="lease_duration sets when a lease expires"):
        lease = SoftFileLease(str(marker), lease_duration=_DURATION, lifetime=5)

    assert lease.lifetime is None


def test_native_lock_rejects_a_lease_duration(marker: Path) -> None:
    # A kernel lock lives on the inode, so no pathname age can revoke it; the option must not even be accepted. The
    # rejection happens at runtime, so reach it the way a caller without a type checker would.
    construct = cast("Callable[..., FileLock]", FileLock)

    with pytest.raises(TypeError, match="does not support non-default lock options: lease_duration"):
        construct(str(marker), lease_duration=5)
