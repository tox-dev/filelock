from __future__ import annotations

import gc
import itertools
import os
import socket
import subprocess  # ruff:ignore[suspicious-subprocess-import]  # interpreter exit finalizes a held lease
import sys
import threading
import time
from contextlib import suppress
from errno import EIO, ENOENT
from threading import Thread
from types import SimpleNamespace
from typing import TYPE_CHECKING, Final, Literal, TypedDict, cast

import pytest

from filelock import (
    AsyncSoftFileLease,
    FileLock,
    LeaseCompromise,
    LeaseSettingsMismatch,
    SoftFileLease,
    StrictSoftFileLock,
    Timeout,
)
from filelock._identity import host_name
from filelock._soft import _MALFORMED_LOCK_AGE_THRESHOLD
from tests.capability_marks import NEEDS_COLLECTED_FINALIZATION, NEEDS_PROMPT_FINALIZATION, NEEDS_UNLINK_OPEN_FILE

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from pytest_mock import MockerFixture

#: Short enough to keep the suite quick, long enough that a loaded runner still refreshes twice before expiry.
_DURATION: float = 0.9
_HEARTBEAT: float = 0.1

#: Taking a claim from a live holder means removing its marker while the holder still has it open. Where that is
#: refused, a lease only reclaims once the holder exits and its handle closes.


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
            host_name(),
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


def test_lease_token_names_no_claim_after_a_contended_acquire(marker: Path) -> None:
    contender = _lease(marker, timeout=0)

    with _lease(marker):
        with pytest.raises(Timeout):
            contender.acquire()
        assert contender.token is None


def test_lease_token_names_no_claim_after_an_acquire_that_raises(tmp_path: Path) -> None:
    # The one path no rollback covers: a raise before the acquire holds a descriptor.
    (blocker := tmp_path / "blocker").touch()
    lease = _lease(blocker / "resource.lock")

    with pytest.raises(FileExistsError):
        lease.acquire()

    assert lease.token is None


def test_lease_heartbeat_keeps_a_live_claim_past_its_duration(marker: Path) -> None:
    holder = _lease(marker)

    with holder:
        time.sleep(_DURATION * 1.5)  # only a refreshing heartbeat keeps the claim past this
        with pytest.raises(Timeout):
            _lease(marker).acquire()


@NEEDS_UNLINK_OPEN_FILE  # pragma: needs unlink-open-file
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


def test_lease_self_heals_a_malformed_marker(marker: Path, mocker: MockerFixture) -> None:
    # A partial write or a foreign file leaves a marker the lease parser cannot read. Rather than block every
    # contender until timeout, the base self-heal evicts it once it ages past the malformed grace window.
    mocker.patch("filelock._soft.time.monotonic", side_effect=itertools.count(step=_MALFORMED_LOCK_AGE_THRESHOLD))
    marker.write_text("not a protocol 2 record\n", encoding="utf-8")

    with _lease(marker) as lease:
        assert lease.is_lock_held_by_us


@pytest.mark.parametrize(
    "contract",
    [
        pytest.param(f"mode=lease\ntoken=abc\nduration={_DURATION!r}\n", id="lease"),
        pytest.param("mode=exclusive\n", id="exclusive"),
    ],
)
def test_lease_reclaims_a_dead_same_host_holder(marker: Path, contract: str) -> None:
    marker.write_text(f"filelock/2\npid=999999\nhost={host_name()}\n{contract}", encoding="utf-8")

    with _lease(marker) as lease:
        assert lease.is_lock_held_by_us


@NEEDS_UNLINK_OPEN_FILE
def test_lease_reports_compromise_when_the_marker_vanishes(marker: Path) -> None:  # pragma: needs unlink-open-file
    seen: list[LeaseCompromise] = []
    lease = _lease(marker, on_compromise=seen.append)

    with lease:
        token = lease.token
        marker.unlink()
        time.sleep(_HEARTBEAT * 5)

    assert [(c.reason, c.token, c.lock_file) for c in seen] == [("marker-missing", token, str(marker))]


@NEEDS_UNLINK_OPEN_FILE
def test_lease_reports_compromise_when_a_peer_takes_over(marker: Path) -> None:  # pragma: needs unlink-open-file
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
        # A starved heartbeat can miss one refresh margin; the failure is deduplicated, so it still reports once.
        deadline = time.monotonic() + _DURATION * 20
        while not seen and time.monotonic() < deadline:
            time.sleep(_HEARTBEAT)

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
    # A long lease keeps a slow runner's two failed ticks inside the window; the deadline has its own test above.
    lease = _lease(marker, lease_duration=30, on_compromise=seen.append)
    with lease:
        time.sleep(_HEARTBEAT * 6)  # several ticks: the first two fail, the rest recover
        assert seen == []
        assert lease.compromise is None


@NEEDS_UNLINK_OPEN_FILE
def test_lease_reports_one_compromise_per_claim(marker: Path) -> None:  # pragma: needs unlink-open-file
    seen: list[LeaseCompromise] = []
    lease = _lease(marker, on_compromise=seen.append)

    with lease:
        marker.unlink()
        time.sleep(_HEARTBEAT * 6)  # several refreshes fail, but the holder is told once

    assert len(seen) == 1


@NEEDS_UNLINK_OPEN_FILE
def test_lease_records_the_compromise_without_a_callback(marker: Path) -> None:  # pragma: needs unlink-open-file
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


@NEEDS_UNLINK_OPEN_FILE
def test_lease_can_be_released_from_the_compromise_callback(marker: Path) -> None:  # pragma: needs unlink-open-file
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


@NEEDS_UNLINK_OPEN_FILE  # pragma: needs unlink-open-file
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


def test_lease_records_a_compromise_without_a_callback_deterministically(marker: Path, mocker: MockerFixture) -> None:
    # With no callback the heartbeat still records the loss on the lease so a holder can poll .compromise. A refresh
    # that keeps failing drives the loss on every platform without depending on a peer taking the marker.
    mocker.patch("filelock._lease.touch", side_effect=OSError("cannot touch the marker"))
    lease = _lease(marker)

    with lease:
        deadline = time.monotonic() + _DURATION * 20
        while lease.compromise is None and time.monotonic() < deadline:
            time.sleep(_HEARTBEAT)

    assert lease.compromise is not None
    assert lease.compromise.reason == "refresh-failed"


def test_lease_release_from_the_callback_does_not_join_itself(marker: Path, mocker: MockerFixture) -> None:
    # The callback runs on the heartbeat thread, so releasing there must skip joining that thread onto itself. A shared
    # context lets the thread see the claim, and a failing refresh drives the loss deterministically on every platform.
    mocker.patch("filelock._lease.touch", side_effect=OSError("cannot touch the marker"))
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

    deadline = time.monotonic() + _DURATION * 20
    while lease.is_locked and time.monotonic() < deadline:
        time.sleep(_HEARTBEAT)

    assert lease.compromise is not None
    assert not lease.is_locked


def test_lease_keeps_its_claim_when_another_thread_fails_to_acquire(marker: Path) -> None:
    # The context is thread-local, so a second thread contending on the same lease object must leave the holder's
    # claim alone: a torn-down heartbeat stops refreshing the marker and a peer takes it while the holder still holds.
    holder = _lease(marker)

    def contend() -> None:
        with suppress(Timeout):
            holder.acquire()

    with holder:
        token = holder.token
        contender = Thread(target=contend)
        contender.start()
        contender.join()

        assert holder.token == token
        time.sleep(_DURATION * 1.5)  # only a surviving heartbeat keeps the claim past this
        with pytest.raises(Timeout):
            _lease(marker).acquire()


def test_lease_hands_back_the_claim_when_the_heartbeat_cannot_start(marker: Path, mocker: MockerFixture) -> None:
    # A heartbeat thread the OS refuses (an rlimit reached) must not leave the claim behind: a peer would evict the
    # unrefreshed marker and acquire while this instance still believed it held the lease, and the acquire rollback
    # would raise joining a thread that never started.
    mocker.patch.object(Thread, "start", side_effect=RuntimeError("can't start new thread"))
    lease = _lease(marker)
    with pytest.raises(RuntimeError, match="can't start new thread"):
        lease.acquire()

    assert not lease.is_locked
    assert not marker.exists()
    lease.release(force=True)  # nothing was left to release, so this must not raise


def test_lease_release_stops_a_heartbeat_recorded_before_its_thread_starts(marker: Path, mocker: MockerFixture) -> None:
    # A shared, non-thread-local claim lets a release() run while an acquire has recorded its heartbeat but not yet
    # started the thread. Stubbing start() as a no-op freezes that window: release() must stop the heartbeat without
    # raising on a join of the unstarted thread, and must still unlink the marker so a peer cannot take a claim we
    # let go.
    mocker.patch.object(Thread, "start")
    lease = SoftFileLease(
        str(marker), timeout=0.3, lease_duration=_DURATION, heartbeat_interval=_HEARTBEAT, thread_local=False
    )
    lease.acquire()
    assert lease.is_locked

    lease.release()  # must not raise joining a thread that never started

    assert not lease.is_locked
    assert not marker.exists()


def test_lease_rejects_a_peer_configured_with_another_duration(marker: Path) -> None:
    holder = _lease(marker)

    with holder, pytest.raises(LeaseSettingsMismatch, match="must agree on lease_duration"):
        _lease(marker, lease_duration=_DURATION * 3).acquire()


class _LeaseSettings(TypedDict, total=False):
    lease_duration: float
    heartbeat_interval: float
    on_compromise: Callable[[LeaseCompromise], None]


_LEASE_TYPES: Final = [pytest.param(SoftFileLease, id="sync"), pytest.param(AsyncSoftFileLease, id="async")]


@pytest.fixture
def lease_settings() -> _LeaseSettings:
    return {"lease_duration": 6, "heartbeat_interval": 1, "on_compromise": lambda _compromise: None}


@pytest.mark.parametrize("lease_type", _LEASE_TYPES)
def test_lease_singleton_reuses_the_instance_for_the_same_settings(
    marker: Path, lease_type: type[SoftFileLease], lease_settings: _LeaseSettings
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
    lease_settings: _LeaseSettings,
    setting: Literal["lease_duration", "heartbeat_interval", "on_compromise"],
) -> None:
    _held = lease_type(str(marker), is_singleton=True, **lease_settings)  # the cache only keeps it weakly
    del lease_settings[setting]
    with pytest.raises(ValueError, match=rf"\n\t{setting} \(existing lock has "):
        lease_type(str(marker), is_singleton=True, **lease_settings)


@pytest.mark.requires_hard_links
def test_lease_does_not_expire_a_strict_holder(marker: Path) -> None:  # pragma: needs hard-link
    # A strict holder never agreed to be superseded by age, so a lease contender waits it out instead.
    with StrictSoftFileLock(str(marker)):
        with pytest.raises(Timeout):
            _lease(marker, timeout=_DURATION * 2).acquire()
        assert marker.exists()


@pytest.mark.parametrize(
    ("lease_duration", "heartbeat_interval", "error", "message"),
    [
        pytest.param(0, None, ValueError, "lease_duration must be positive", id="zero-duration"),
        pytest.param(-1, None, ValueError, "lease_duration must be positive", id="negative-duration"),
        pytest.param(float("nan"), _HEARTBEAT, ValueError, "positive and finite", id="nan-duration"),
        pytest.param(float("inf"), _HEARTBEAT, ValueError, "positive and finite", id="infinite-duration"),
        pytest.param(float("-inf"), _HEARTBEAT, ValueError, "positive and finite", id="negative-infinite-duration"),
        pytest.param(True, None, TypeError, "number, not bool", id="true-duration"),
        pytest.param(False, None, TypeError, "number, not bool", id="false-duration"),
        pytest.param(_DURATION, 0, ValueError, "heartbeat_interval must be positive", id="zero-heartbeat"),
        pytest.param(_DURATION, _DURATION, ValueError, "below lease_duration", id="heartbeat-equals-duration"),
        pytest.param(_DURATION, _DURATION * 2, ValueError, "below lease_duration", id="heartbeat-over-duration"),
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
        lease = SoftFileLease(str(marker), lease_duration=_DURATION, lifetime=5)

    assert lease.lifetime is None


def test_lease_supersedes_a_live_holder_once_its_claim_ages_out(marker: Path, mocker: MockerFixture) -> None:
    # A live holder whose refresh stalled keeps its marker, yet a contender takes it once it has watched the marker go
    # lease_duration without a refresh. Windows cannot delete a live holder's open marker, so drive the age branch with
    # a non-stale owner and a clock that steps a full duration per look rather than a real second process.
    mocker.patch("filelock._lease.owner_is_stale", return_value=False)
    mocker.patch("filelock._soft.time.monotonic", side_effect=itertools.count(step=_DURATION))
    marker.write_text(
        f"filelock/2\npid={os.getpid()}\nhost={socket.gethostname()}\nmode=lease\ntoken=stalled\nduration={_DURATION!r}\n",
        encoding="utf-8",
    )

    # The stepped clock ages the marker out on the second look; the real timeout only has to outlast two polls.
    with _lease(marker, timeout=30) as contender:
        assert contender.is_lock_held_by_us
        assert contender.token not in {None, "stalled"}


def test_lease_reports_marker_missing_when_a_refresh_cannot_stat_it(marker: Path, mocker: MockerFixture) -> None:
    # A vanished marker surfaces as FileNotFoundError from the refresh stat. Windows keeps an open marker undeletable,
    # so inject the missing stat at the agnostic os.lstat call rather than unlinking a held file.
    seen: list[LeaseCompromise] = []
    lease = _lease(marker, on_compromise=seen.append)
    lease.acquire()
    token = lease.token
    real_lstat = cast("Callable[..., os.stat_result]", os.lstat)
    missing = False

    def lstat(path: object, *args: object, **kwargs: object) -> object:
        if missing and str(path).endswith(marker.name):
            raise FileNotFoundError(ENOENT, "No such file or directory", marker.name)
        return real_lstat(path, *args, **kwargs)

    mocker.patch("filelock._lease.os.lstat", side_effect=lstat)
    try:
        missing = True
        time.sleep(_HEARTBEAT * 4)
    finally:
        missing = False
        lease.release()

    assert [(c.reason, c.token, c.lock_file) for c in seen] == [("marker-missing", token, str(marker))]


def test_lease_reports_owner_changed_when_the_marker_identity_shifts(marker: Path, mocker: MockerFixture) -> None:
    # A peer that took the expired claim replaces the marker, so the refresh stat names a different inode. Drive that
    # agnostically by returning a stat whose identity differs from the one the holder verified at acquire.
    seen: list[LeaseCompromise] = []
    lease = _lease(marker, on_compromise=seen.append)
    lease.acquire()
    token = lease.token
    real_lstat = cast("Callable[..., os.stat_result]", os.lstat)
    shifted = False

    def lstat(path: object, *args: object, **kwargs: object) -> object:
        st = real_lstat(path, *args, **kwargs)
        if shifted and str(path).endswith(marker.name):
            return SimpleNamespace(st_dev=st.st_dev, st_ino=st.st_ino + 1)
        return st

    mocker.patch("filelock._lease.os.lstat", side_effect=lstat)
    try:
        shifted = True
        time.sleep(_HEARTBEAT * 4)
    finally:
        shifted = False
        lease.release()

    assert [(c.reason, c.token) for c in seen] == [("owner-changed", token)]


def test_native_lock_rejects_a_lease_duration(marker: Path) -> None:
    # A kernel lock lives on the inode, so no pathname age can revoke it; the option must not even be accepted. The
    # rejection happens at runtime, so reach it the way a caller without a type checker would.
    construct = cast("Callable[..., FileLock]", FileLock)

    with pytest.raises(TypeError, match="does not support non-default lock options: lease_duration"):
        construct(str(marker), lease_duration=5)


def test_lease_does_not_age_out_a_live_exclusive_owner(marker: Path, mocker: MockerFixture) -> None:
    # An exclusive owner never agreed to expire, so only proof of death may reclaim it, however long it sits unchanged.
    mocker.patch("filelock._lease.owner_is_stale", return_value=False)
    mocker.patch("filelock._soft.time.monotonic", side_effect=itertools.count(step=_DURATION))
    marker.write_text(f"filelock/2\npid={os.getpid()}\nhost={socket.gethostname()}\nmode=exclusive\n", encoding="utf-8")

    with pytest.raises(Timeout):
        _lease(marker).acquire()

    assert marker.exists()


def test_lease_keeps_a_claim_refreshed_by_a_host_whose_clock_lags(marker: Path, mocker: MockerFixture) -> None:
    # The holder's clock runs an hour behind, so every refresh stamps an hour-old mtime. The claim is live as long as
    # the mtime keeps changing; reading it against this host's wall clock would supersede the holder at once.
    mocker.patch("filelock._lease.owner_is_stale", return_value=False)
    marker.write_text(
        f"filelock/2\npid={os.getpid()}\nhost={socket.gethostname()}\nmode=lease\ntoken=skewed\nduration={_DURATION!r}\n",
        encoding="utf-8",
    )
    stop = threading.Event()

    def refresh_an_hour_behind() -> None:
        while not stop.wait(_HEARTBEAT):
            lagging = time.time() - 3600
            os.utime(marker, (lagging, lagging))

    refresher = Thread(target=refresh_an_hour_behind)
    refresher.start()
    try:
        with pytest.raises(Timeout):
            _lease(marker, timeout=_DURATION * 3).acquire()
    finally:
        stop.set()
        refresher.join()

    assert marker.read_text(encoding="utf-8").endswith("token=skewed\nduration=0.9\n")


def _lease_heartbeats() -> set[threading.Thread]:
    return {thread for thread in threading.enumerate() if thread.name.startswith("filelock-lease-")}


@NEEDS_COLLECTED_FINALIZATION
def test_lease_dropped_while_held_releases_its_marker(marker: Path) -> None:
    lease = _lease(marker)
    lease.acquire()

    del lease
    gc.collect()

    assert not marker.exists()


@NEEDS_COLLECTED_FINALIZATION
def test_lease_dropped_where_it_cannot_be_released_stops_its_heartbeat(marker: Path) -> None:
    # The thread-local context hides the claim from the thread that drops the last reference, so __del__ there cannot
    # release it; the heartbeat must still stop, letting the unrefreshed claim age out for a peer.
    before = _lease_heartbeats()
    leases: list[SoftFileLease] = []

    def acquire() -> None:
        lease = _lease(marker)
        lease.acquire()
        leases.append(lease)

    acquirer = Thread(target=acquire)
    acquirer.start()
    acquirer.join()
    heartbeats = _lease_heartbeats() - before
    assert heartbeats

    leases.clear()
    # A tracing collector finalizes the lease on the first pass and runs the weak reference callback on the next.
    gc.collect()
    gc.collect()

    for heartbeat in heartbeats:
        heartbeat.join(timeout=_DURATION * 20)
    assert not any(heartbeat.is_alive() for heartbeat in heartbeats)


# Interpreter exit finalizes module globals by dropping their references, which only a refcounting collector turns into
# __del__ calls.
@NEEDS_PROMPT_FINALIZATION
def test_lease_held_at_interpreter_exit_releases_its_marker(marker: Path) -> None:
    script = "import sys; from filelock import SoftFileLease; lease = SoftFileLease(sys.argv[1]); lease.acquire()"

    subprocess.run([sys.executable, "-c", script, str(marker)], check=True, timeout=10)

    assert not marker.exists()


@NEEDS_COLLECTED_FINALIZATION
def test_lease_dropped_while_its_heartbeat_finds_the_claim_lost_reports_nothing(
    marker: Path, mocker: MockerFixture
) -> None:
    # The last reference goes while the heartbeat is mid-refresh, past the wait the collection would have stopped, and
    # the refresh then finds the marker gone. There is no holder left to tell, so the heartbeat just ends.
    seen: list[LeaseCompromise] = []
    leases = [
        SoftFileLease(
            str(marker),
            thread_local=False,
            lease_duration=_DURATION,
            heartbeat_interval=_HEARTBEAT,
            on_compromise=seen.append,
        )
    ]
    before = _lease_heartbeats()
    leases[0].acquire()
    heartbeats = _lease_heartbeats() - before
    real_lstat = cast("Callable[..., os.stat_result]", os.lstat)

    def lstat(path: object, *args: object, **kwargs: object) -> object:
        if leases and str(path).endswith(marker.name):
            leases.clear()
            gc.collect()
            raise FileNotFoundError(ENOENT, "No such file or directory", marker.name)
        return real_lstat(path, *args, **kwargs)

    mocker.patch("filelock._lease.os.lstat", side_effect=lstat)
    for heartbeat in heartbeats:
        heartbeat.join(timeout=_DURATION * 20)

    assert (leases, seen) == ([], [])
    assert not any(heartbeat.is_alive() for heartbeat in heartbeats)
