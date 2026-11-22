from __future__ import annotations

import itertools
import os
import sys
import time
from errno import EIO, ENOENT
from threading import Event, Semaphore
from typing import TYPE_CHECKING, Final, Protocol

import pytest

import filelock._soft._lease as lease_mod
from filelock import (
    SoftFileLease,
    Timeout,
)
from filelock._filesystem import exchange_names
from filelock._identity import host_name
from filelock._soft import MALFORMED_LOCK_AGE_THRESHOLD
from tests.capability_marks import (
    NEEDS_STALE_RECLAIM,
    NEEDS_UNLINK_OPEN_FILE,
)
from tests.conftest import held_after_reclaim

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from pytest_mock import MockerFixture

from tests.soft.lease_helpers import DEADLINE, DURATION, Reports, make_lease, wait_until


def test_lease_heartbeat_keeps_a_live_claim_past_its_duration(marker: Path) -> None:

    with make_lease(marker):
        time.sleep(  # simplint: ignore[sleep-in-test]  # needs a clock hook for lease duration
            DURATION * 1.5
        )  # only a refreshing heartbeat keeps the claim past this
        with pytest.raises(Timeout):
            make_lease(marker).acquire()


def test_lease_heartbeat_uses_acquisition_directory(
    working_directories: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    first, second = working_directories
    compromised = Event()
    lease = SoftFileLease(
        "resource.lock", lease_duration=0.9, heartbeat_interval=0.05, on_compromise=lambda _: compromised.set()
    )
    with lease:
        monkeypatch.chdir(second)
        os.utime(first / "resource.lock", (0, 0))

        assert not compromised.wait(timeout=0.3)
        assert (first / "resource.lock").stat().st_mtime > 0


@pytest.mark.skipif(
    sys.platform == "win32", reason="Windows refuses to rename a directory holding an open file"
)  # pragma: win32 no cover
def test_lease_heartbeat_survives_directory_rename(
    working_directories: tuple[Path, Path],
) -> None:
    first, _ = working_directories
    renamed = first.parent / "renamed"
    compromised = Event()
    lease = SoftFileLease(
        "resource.lock", lease_duration=0.9, heartbeat_interval=0.05, on_compromise=lambda _: compromised.set()
    )
    with lease:
        first.rename(renamed)
        os.utime(renamed / "resource.lock", (0, 0))

        assert not compromised.wait(timeout=0.3)
        assert (renamed / "resource.lock").stat().st_mtime > 0


@NEEDS_UNLINK_OPEN_FILE  # pragma: needs unlink-open-file
@NEEDS_STALE_RECLAIM
def test_lease_peer_takes_an_expired_claim(marker: Path, mocker: MockerFixture) -> None:
    # A wedged holder: its marker stays on disk, but no refresh ever lands on it again, so the claim ages out.
    mocker.patch("filelock._soft._lease.touch", new=mocker.create_autospec(lease_mod.touch))
    holder = make_lease(marker)
    holder.acquire()

    try:
        peer = make_lease(marker, timeout=DURATION * 5)
        with peer:
            assert peer.is_lock_held_by_us
            assert peer.token != holder.token
    finally:
        holder.release()


@NEEDS_STALE_RECLAIM
def test_lease_self_heals_a_malformed_marker(marker: Path, mocker: MockerFixture) -> None:
    # The pytest timeout bounds recovery while the mocked clock controls marker expiry.
    clock = mocker.patch("filelock._soft._exclusive._recovery.time", autospec=True)
    clock.monotonic.side_effect = itertools.count(step=MALFORMED_LOCK_AGE_THRESHOLD)
    marker.write_text("not a protocol 2 record\n", encoding="utf-8")

    with make_lease(marker, timeout=-1) as lease:
        assert lease.is_lock_held_by_us


@pytest.mark.parametrize(
    "contract",
    [
        pytest.param(f"mode=lease\ntoken=abc\nduration={DURATION!r}\n", id="lease"),
        pytest.param(f"mode=lease\ntoken=abc\nduration={DURATION * 3!r}\n", id="lease-other-duration"),
        pytest.param("mode=exclusive\n", id="exclusive"),
    ],
)
@NEEDS_STALE_RECLAIM
def test_lease_reclaims_a_dead_same_host_holder(marker: Path, contract: str) -> None:
    marker.write_text(f"filelock/2\npid=999999\nhost={host_name()}\n{contract}", encoding="utf-8")

    with held_after_reclaim(make_lease(marker)) as lease:
        assert lease.is_lock_held_by_us


@NEEDS_UNLINK_OPEN_FILE
def test_lease_reports_compromise_when_the_marker_vanishes(marker: Path) -> None:  # pragma: needs unlink-open-file
    reports = Reports()
    lease = make_lease(marker, on_compromise=reports)

    with lease:
        token = lease.token
        marker.unlink()
        assert reports.landed.wait(DEADLINE)

    assert [(c.reason, c.token, c.lock_file) for c in reports.seen] == [("marker-missing", token, str(marker))]


@NEEDS_UNLINK_OPEN_FILE  # pragma: needs unlink-open-file
@pytest.mark.requires_hard_links  # pragma: needs hard-link
def test_lease_reports_missing_marker_with_another_hard_link(marker: Path) -> None:
    reports = Reports()

    with make_lease(marker, on_compromise=reports):
        os.link(marker, other := marker.with_name("other"))
        marker.unlink()
        assert reports.landed.wait(DURATION * 3)
        assert other.exists()

    assert [compromise.reason for compromise in reports.seen] == ["marker-missing"]


@pytest.mark.skipif(
    sys.platform == "win32", reason="Windows refuses to rename a directory holding an open file"
)  # pragma: win32 no cover
def test_lease_retries_when_missing_markers_parent_cannot_be_read(tmp_path: Path, mocker: MockerFixture) -> None:
    marker = tmp_path / "original" / "a.lock"
    marker.parent.mkdir()
    reports = Reports()
    lease = make_lease(marker, on_compromise=reports)
    lease.acquire()
    marker.parent.rename(tmp_path / "renamed")
    real_lstat = os.lstat

    def lstat(path: str | os.PathLike[str]) -> os.stat_result:
        if os.fspath(path) == str(marker.parent):
            raise OSError(EIO, "Input/output error")
        return real_lstat(path)

    mocker.patch("filelock._soft._lease.os.lstat", autospec=True, side_effect=lstat)
    try:
        assert reports.landed.wait(DEADLINE)
    finally:
        mocker.stopall()
        lease.release()

    assert len(reports.seen) == 1
    assert isinstance((error := reports.seen[0].error), OSError)
    assert (reports.seen[0].reason, error.errno) == ("refresh-failed", EIO)


@pytest.mark.skipif(sys.platform != "win32", reason="requires a Windows directory junction")  # pragma: win32 cover
@pytest.mark.parametrize(
    "parent_errno", [pytest.param(None, id="missing-parent"), pytest.param(EIO, id="unreadable-parent")]
)
def test_lease_reports_refresh_failure_after_junction_removed(
    tmp_path: Path, create_junction: Callable[[Path, Path], None], mocker: MockerFixture, parent_errno: int | None
) -> None:
    target = tmp_path / "target"
    target.mkdir()
    junction = tmp_path / "junction"
    create_junction(junction, target)
    marker = junction / "a.lock"
    reports = Reports()
    lease = make_lease(marker, on_compromise=reports)
    lease.acquire()
    real_lstat = os.lstat

    def lstat(path: str | os.PathLike[str]) -> os.stat_result:
        if os.fspath(path) == str(junction) and parent_errno is not None:
            raise OSError(parent_errno, os.strerror(parent_errno), str(junction))
        return real_lstat(path)

    mocker.patch("filelock._soft._lease.os.lstat", autospec=True, side_effect=lstat)

    try:
        junction.rmdir()
        assert (target / marker.name).exists()
        assert reports.landed.wait(DEADLINE)
    finally:
        mocker.stopall()
        lease.release()

    assert len(reports.seen) == 1
    assert isinstance((error := reports.seen[0].error), OSError)
    assert (reports.seen[0].reason, error.errno) == ("refresh-failed", parent_errno or ENOENT)


@NEEDS_UNLINK_OPEN_FILE
def test_lease_reports_compromise_when_a_peer_takes_over(marker: Path) -> None:  # pragma: needs unlink-open-file
    reports = Reports()
    holder = make_lease(marker, on_compromise=reports)
    peer = make_lease(marker)

    try:
        with holder:
            marker.unlink()
            peer.acquire()  # a peer publishes a fresh marker at the same path
            assert reports.landed.wait(DEADLINE)
        assert [c.reason for c in reports.seen] == ["owner-changed"]
    finally:
        peer.release()  # stop the peer's heartbeat here, so its release log never lands in a later test's caplog


def test_lease_reports_compromise_when_a_refresh_fails(marker: Path, mocker: MockerFixture) -> None:
    reports = Reports()
    failure = OSError("cannot touch the marker")
    mocker.patch("filelock._soft._lease.touch", side_effect=failure, autospec=True)
    lease = make_lease(marker, on_compromise=reports)

    with lease:
        # A starved heartbeat can miss one refresh margin; the failure is deduplicated, so it still reports once.
        assert reports.landed.wait(DEADLINE)

    assert [(c.reason, c.error) for c in reports.seen] == [("refresh-failed", failure)]


class _RefreshCall(Protocol):
    def __call__(self, path: str | os.PathLike[str], *args: object, **kwargs: object) -> os.stat_result | None: ...


@pytest.mark.parametrize(
    ("target", "real"),
    [pytest.param("touch", lease_mod.touch, id="touch"), pytest.param("os.lstat", os.lstat, id="lstat")],
)
def test_lease_tolerates_a_transient_refresh_error(
    marker: Path, mocker: MockerFixture, target: str, real: _RefreshCall
) -> None:
    # A refresh can recover while the previously observed lease is still valid.
    ticks = itertools.count()
    refreshes: Final[Semaphore] = Semaphore(0)

    def flaky(path: str | os.PathLike[str], *args: object, **kwargs: object) -> os.stat_result | None:
        marked = os.fspath(path).endswith(marker.name)
        try:
            if marked and next(ticks) < 2:
                raise OSError(EIO, "Input/output error")
            return real(path, *args, **kwargs)
        finally:
            if marked:
                refreshes.release()

    mocker.patch(f"filelock._soft._lease.{target}", side_effect=flaky, autospec=True)
    reports = Reports()
    # A long lease keeps a slow runner's two failed ticks inside the window; the deadline has its own test above.
    lease: Final[SoftFileLease] = make_lease(marker, lease_duration=30, on_compromise=reports)
    with lease:
        for _ in range(6):  # the first two fail, the rest recover
            assert refreshes.acquire(timeout=DEADLINE)
        assert reports.seen == []
        assert lease.compromise is None


@pytest.mark.skipif(sys.platform == "win32", reason="Windows cannot exchange two names")
@NEEDS_STALE_RECLAIM
def test_lease_heartbeat_tolerates_one_refresh_under_a_peers_exchange(  # pragma: win32 no cover
    marker: Path, mocker: MockerFixture
) -> None:
    reports = Reports()
    lease: Final[SoftFileLease] = SoftFileLease(
        marker, lease_duration=2, heartbeat_interval=0.05, on_compromise=reports
    )
    refreshed: Final[Semaphore] = Semaphore(0)
    lstat = os.lstat

    def spy(path: str | os.PathLike[str], *, dir_fd: int | None = None) -> os.stat_result:
        found: Final[os.stat_result] = lstat(path, dir_fd=dir_fd)
        if os.fspath(path) == str(marker):  # pragma: no branch  # guard: only the lease refresh calls lstat here
            refreshed.release()
        return found

    with lease:
        (peer := marker.with_name("peer")).write_text("peer", encoding="utf-8")
        exchange_names(str(marker), str(peer))
        mocker.patch.object(os, "lstat", new=spy)
        assert refreshed.acquire(timeout=DEADLINE)
        exchange_names(str(marker), str(peer))
        for _ in range(2):
            assert refreshed.acquire(timeout=DEADLINE)
        mocker.stopall()

        assert (lease.compromise, reports.seen, lease.is_locked) == (None, [], True)


@pytest.mark.skipif(sys.platform == "win32", reason="Windows cannot exchange two names")
@NEEDS_STALE_RECLAIM
def test_lease_heartbeat_reports_an_owner_changed_that_persists(marker: Path) -> None:  # pragma: win32 no cover
    reports = Reports()
    lease: Final[SoftFileLease] = SoftFileLease(
        marker, lease_duration=2, heartbeat_interval=0.05, on_compromise=reports
    )

    with lease:
        (peer := marker.with_name("peer")).write_text("peer", encoding="utf-8")
        exchange_names(str(marker), str(peer))
        assert reports.landed.wait(DEADLINE)
        exchange_names(str(marker), str(peer))

    assert [compromise.reason for compromise in reports.seen] == ["owner-changed"]


@NEEDS_UNLINK_OPEN_FILE
def test_lease_reports_one_compromise_per_claim(marker: Path) -> None:  # pragma: needs unlink-open-file
    reports = Reports()

    with make_lease(marker, on_compromise=reports):
        marker.unlink()
        assert reports.landed.wait(DEADLINE)

    assert len(reports.seen) == 1


@NEEDS_UNLINK_OPEN_FILE
def test_lease_records_the_compromise_without_a_callback(marker: Path) -> None:  # pragma: needs unlink-open-file
    lease = make_lease(marker)

    with lease:
        marker.unlink()
        wait_until(lambda: lease.compromise is not None)
        compromise = lease.compromise

    assert compromise is not None
    assert compromise.reason == "marker-missing"
