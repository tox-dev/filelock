from __future__ import annotations

import os
import sys
import threading
from errno import EIO
from threading import Event, Semaphore
from typing import TYPE_CHECKING, Final

import pytest

import filelock._soft._lease as lease_mod
from filelock import (
    LeaseCompromise,
    SoftFileLease,
)
from tests.capability_marks import (
    NEEDS_UNLINK_OPEN_FILE,
)

if TYPE_CHECKING:
    from pathlib import Path

    from pytest_mock import MockerFixture

from tests.soft.lease_helpers import DEADLINE, DURATION, HEARTBEAT, Reports, make_lease, wait_until


@NEEDS_UNLINK_OPEN_FILE  # pragma: needs unlink-open-file
def test_lease_release_records_a_missing_marker_before_the_next_heartbeat(
    marker: Path,
) -> None:
    reports = Reports()
    lease = SoftFileLease(marker, lease_duration=30, heartbeat_interval=29, on_compromise=reports)

    with lease:
        token = lease.token
        assert token is not None
        marker.unlink()

    assert lease.compromise == LeaseCompromise(lock_file=str(marker), token=token, reason="marker-missing")
    assert reports.seen == []


@NEEDS_UNLINK_OPEN_FILE
def test_lease_release_records_a_replacement_before_the_next_heartbeat(  # pragma: needs unlink-open-file
    marker: Path,
) -> None:
    reports = Reports()
    lease = SoftFileLease(marker, lease_duration=30, heartbeat_interval=29, on_compromise=reports)
    peer = SoftFileLease(marker, timeout=0, lease_duration=30, heartbeat_interval=29)

    try:
        with lease:
            token = lease.token
            assert token is not None
            marker.unlink()
            peer.acquire()
        assert lease.compromise == LeaseCompromise(lock_file=str(marker), token=token, reason="owner-changed")
        assert reports.seen == []
    finally:
        peer.release()


@pytest.mark.requires_hard_links  # pragma: needs hard-link
@NEEDS_UNLINK_OPEN_FILE
def test_lease_release_records_a_missing_marker_with_an_extra_link(  # pragma: needs unlink-open-file
    marker: Path,
) -> None:
    lease = SoftFileLease(marker, lease_duration=30, heartbeat_interval=29)
    alias = marker.with_name("alias")

    with lease:
        token = lease.token
        assert token is not None
        os.link(marker, alias)
        marker.unlink()

    assert lease.compromise == LeaseCompromise(lock_file=str(marker), token=token, reason="marker-missing")
    alias.unlink()


@NEEDS_UNLINK_OPEN_FILE
def test_lease_release_records_a_replacement_with_an_unrelated_break_name(  # pragma: needs unlink-open-file
    marker: Path,
) -> None:
    lease = SoftFileLease(marker, lease_duration=30, heartbeat_interval=29)
    peer = SoftFileLease(marker, timeout=0, lease_duration=30, heartbeat_interval=29)
    alias = marker.with_name("alias")
    unrelated_break = marker.with_name(f"{marker.name}.break.{os.getpid()}.00000000000000000000000000000000")
    unrelated_break.write_text("unrelated", encoding="utf-8")

    try:
        with lease:
            token = lease.token
            assert token is not None
            marker.rename(alias)
            peer.acquire()
        assert lease.compromise == LeaseCompromise(lock_file=str(marker), token=token, reason="owner-changed")
    finally:
        peer.release()
        alias.unlink()
        unrelated_break.unlink()


@NEEDS_UNLINK_OPEN_FILE
def test_lease_release_records_loss_after_the_first_heartbeat_miss(  # pragma: needs unlink-open-file
    marker: Path, mocker: MockerFixture
) -> None:
    first_miss = Event()
    reports = Reports()
    lease = SoftFileLease(marker, lease_duration=30, heartbeat_interval=2, on_compromise=reports)
    peer = SoftFileLease(marker, timeout=0, lease_duration=30, heartbeat_interval=29)

    try:
        with lease:
            token = lease.token
            assert token is not None
            held = os.lstat(marker)
            original_lstat = os.lstat
            holder_thread = threading.current_thread()

            def signal_first_miss(path: str | os.PathLike[str], *, dir_fd: int | None = None) -> os.stat_result:
                result = original_lstat(path, dir_fd=dir_fd)
                if (
                    os.fspath(path) == str(marker)
                    and threading.current_thread() is not holder_thread
                    and (result.st_dev, result.st_ino) != (held.st_dev, held.st_ino)
                ):
                    first_miss.set()
                return result

            mocker.patch("filelock._soft._lease.os.lstat", side_effect=signal_first_miss, autospec=True)
            marker.unlink()
            peer.acquire()
            assert first_miss.wait(5)
            assert reports.seen == []
        assert lease.compromise == LeaseCompromise(lock_file=str(marker), token=token, reason="owner-changed")
        assert reports.seen == []
    finally:
        peer.release()


@pytest.mark.skipif(sys.platform == "win32", reason="Windows cannot rename an open marker")
def test_lease_release_does_not_report_a_provisional_break(marker: Path) -> None:  # pragma: win32 no cover
    lease = SoftFileLease(marker, lease_duration=30, heartbeat_interval=29)
    aside = marker.with_name(f"{marker.name}.break.{os.getpid()}.pending")

    with lease:
        marker.rename(aside)
        marker.write_text("peer", encoding="utf-8")

    assert lease.compromise is None


@pytest.mark.skipif(sys.platform == "win32", reason="Windows cannot rename an open marker")
def test_lease_release_does_not_report_a_rolled_back_break(  # pragma: win32 no cover
    marker: Path, mocker: MockerFixture
) -> None:
    lease = SoftFileLease(marker, lease_duration=30, heartbeat_interval=29)
    aside = marker.with_name(f"{marker.name}.break.{os.getpid()}.pending")
    fstat = os.fstat
    first = iter([True])

    def restore(fd: int) -> os.stat_result:
        if next(first, False):
            marker.unlink()
            aside.rename(marker)
        return fstat(fd)

    with lease:
        marker.rename(aside)
        marker.write_text("peer", encoding="utf-8")
        mocker.patch("filelock._soft._lease.os.fstat", side_effect=restore, autospec=True)

    assert lease.compromise is None
    assert not marker.exists()


@pytest.mark.skipif(sys.platform == "win32", reason="Windows cannot rename a directory with an open marker")
def test_lease_release_does_not_report_a_renamed_directory(marker: Path) -> None:  # pragma: win32 no cover
    lease = SoftFileLease(marker, lease_duration=30, heartbeat_interval=29)
    moved = marker.parent.with_name(f"{marker.parent.name}-moved")

    with lease:
        marker.parent.rename(moved)

    assert lease.compromise is None
    (moved / marker.name).unlink()


@pytest.mark.skipif(sys.platform == "win32", reason="Windows cannot rename a directory with an open marker")
def test_lease_release_does_not_report_a_restored_directory(  # pragma: win32 no cover
    marker: Path, mocker: MockerFixture
) -> None:
    lease = SoftFileLease(marker, lease_duration=30, heartbeat_interval=29)
    moved = marker.parent.with_name(f"{marker.parent.name}-moved")
    fstat = os.fstat
    first = iter([True])

    def restore(fd: int) -> os.stat_result:
        if next(first, False):
            moved.rename(marker.parent)
        return fstat(fd)

    with lease:
        marker.parent.rename(moved)
        mocker.patch("filelock._soft._lease.os.fstat", side_effect=restore, autospec=True)

    assert lease.compromise is None
    assert not marker.exists()


@pytest.mark.skipif(sys.platform == "win32", reason="Release probes the marker only on POSIX")
def test_lease_release_does_not_report_an_unreadable_marker(
    marker: Path, mocker: MockerFixture
) -> None:  # pragma: win32 no cover
    lease = SoftFileLease(marker, lease_duration=30, heartbeat_interval=29)
    lstat = os.lstat
    first = iter([True])

    def fail_once(path: str | os.PathLike[str], *, dir_fd: int | None = None) -> os.stat_result:
        if os.fspath(path) == str(marker) and next(first, False):
            raise OSError(EIO, "Input/output error")
        return lstat(path, dir_fd=dir_fd)

    with lease:
        mocker.patch("filelock._soft._lease.os.lstat", side_effect=fail_once, autospec=True)

    assert lease.compromise is None
    assert not marker.exists()


def test_lease_holds_an_uncompromised_claim(marker: Path, mocker: MockerFixture) -> None:
    refreshes = _count_refreshes(mocker)
    lease = make_lease(marker)

    with lease:
        for _ in range(3):
            assert refreshes.acquire(timeout=DEADLINE)
        assert lease.compromise is None


def _count_refreshes(mocker: MockerFixture) -> Semaphore:
    # Releases one permit per refresh the heartbeat makes, so a test waits for ticks rather than sleeping through them.
    refreshes: Final[Semaphore] = Semaphore(0)
    real = lease_mod.touch

    def touch_and_count(name: str, *, fd: int) -> None:
        real(name, fd=fd)
        refreshes.release()

    mocker.patch("filelock._soft._lease.touch", side_effect=touch_and_count, autospec=True)
    return refreshes


@NEEDS_UNLINK_OPEN_FILE
def test_lease_can_be_released_from_the_compromise_callback(marker: Path) -> None:  # pragma: needs unlink-open-file
    # The heartbeat callback can release only a context visible on its thread.
    holder: list[SoftFileLease] = []

    released: Final[Event] = Event()

    def release_the_claim(_: LeaseCompromise) -> None:
        holder[0].release()
        released.set()

    lease = SoftFileLease(
        str(marker),
        thread_local=False,
        lease_duration=DURATION,
        heartbeat_interval=HEARTBEAT,
        on_compromise=release_the_claim,
    )
    holder.append(lease)
    lease.acquire()

    marker.unlink()
    assert released.wait(DEADLINE)

    assert lease.compromise is not None
    assert not lease.is_locked


@NEEDS_UNLINK_OPEN_FILE  # pragma: needs unlink-open-file
def test_lease_release_from_the_callback_needs_a_shared_context(marker: Path) -> None:
    # A thread-local lease belongs to the acquiring thread.
    holder: list[SoftFileLease] = []

    released: Final[Event] = Event()

    def release_the_claim(_: LeaseCompromise) -> None:
        holder[0].release()
        released.set()

    lease = make_lease(marker, on_compromise=release_the_claim)
    holder.append(lease)
    lease.acquire()

    try:
        marker.unlink()
        assert released.wait(DEADLINE)
        assert lease.is_locked, "a thread-local release() from the heartbeat thread silently did nothing"
    finally:
        lease.release()


def test_lease_records_a_compromise_without_a_callback_deterministically(marker: Path, mocker: MockerFixture) -> None:
    # A failed refresh drives loss on platforms that forbid deleting an open marker.
    mocker.patch("filelock._soft._lease.touch", side_effect=OSError("cannot touch the marker"), autospec=True)
    lease = make_lease(marker)

    with lease:
        wait_until(lambda: lease.compromise is not None)

    assert lease.compromise is not None
    assert lease.compromise.reason == "refresh-failed"


def test_lease_release_from_the_callback_does_not_join_itself(marker: Path, mocker: MockerFixture) -> None:
    # The callback runs on the thread that release normally joins.
    mocker.patch("filelock._soft._lease.touch", side_effect=OSError("cannot touch the marker"), autospec=True)
    holder: list[SoftFileLease] = []

    released: Final[Event] = Event()

    def release_the_claim(_: LeaseCompromise) -> None:
        holder[0].release()
        released.set()

    lease = SoftFileLease(
        str(marker),
        thread_local=False,
        lease_duration=DURATION,
        heartbeat_interval=HEARTBEAT,
        on_compromise=release_the_claim,
    )
    holder.append(lease)
    lease.acquire()

    assert released.wait(DEADLINE)

    assert lease.compromise is not None
    assert not lease.is_locked
