from __future__ import annotations

import os
from errno import EIO, ENOENT
from threading import Semaphore
from types import SimpleNamespace
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from pathlib import Path

    from pytest_mock import MockerFixture

    from filelock import (
        SoftFileLease,
    )

from tests.soft.lease_helpers import DEADLINE, Reports, make_lease


def test_lease_reports_marker_missing_when_a_refresh_cannot_stat_it(marker: Path, mocker: MockerFixture) -> None:
    # Windows prevents unlinking a held marker, so simulate its disappearance at lstat.
    reports = Reports()
    lease = make_lease(marker, on_compromise=reports)
    lease.acquire()
    token = lease.token
    real_lstat = os.lstat
    real_fstat = os.fstat
    missing = False

    def lstat(path: str | os.PathLike[str]) -> os.stat_result:
        if missing and os.fspath(path).endswith(marker.name):
            raise FileNotFoundError(ENOENT, "No such file or directory", marker.name)
        return real_lstat(path)

    def fstat(fd: int) -> os.stat_result | SimpleNamespace:
        return SimpleNamespace(st_nlink=0) if missing else real_fstat(fd)

    mocker.patch("filelock._soft._lease.os.lstat", autospec=True, side_effect=lstat)
    mocker.patch("filelock._soft._lease.os.fstat", autospec=True, side_effect=fstat)
    try:
        missing = True
        # The first miss may be a peer's break in flight, so the report comes a refresh later.
        assert reports.landed.wait(DEADLINE)
    finally:
        missing = False
        lease.release()

    assert [(c.reason, c.token, c.lock_file) for c in reports.seen] == [("marker-missing", token, str(marker))]


def test_lease_reports_refresh_failed_when_a_missing_marker_cannot_be_inspected(
    marker: Path, mocker: MockerFixture
) -> None:
    reports = Reports()
    lease = make_lease(marker, on_compromise=reports)
    lease.acquire()
    real_lstat = os.lstat
    real_fstat = os.fstat
    missing = False
    name_lost = False

    def lstat(path: str | os.PathLike[str]) -> os.stat_result:
        nonlocal name_lost
        if missing and os.fspath(path).endswith(marker.name):
            name_lost = True
            raise FileNotFoundError(ENOENT, "No such file or directory", marker.name)
        return real_lstat(path)

    def fstat(fd: int) -> os.stat_result:
        # Fails only once the name is gone, so no other call in the refresh can report first.
        if name_lost:
            raise OSError(EIO, "Input/output error")
        return real_fstat(fd)

    mocker.patch("filelock._soft._lease.os.lstat", autospec=True, side_effect=lstat)
    mocker.patch("filelock._soft._lease.os.fstat", autospec=True, side_effect=fstat)
    try:
        missing = True
        assert reports.landed.wait(DEADLINE)
    finally:
        missing = False
        name_lost = False
        lease.release()

    assert [c.reason for c in reports.seen] == ["refresh-failed"]


def test_lease_rides_out_a_marker_missing_for_one_refresh(marker: Path, mocker: MockerFixture) -> None:
    # A peer's stale break moves a live marker aside and links it back; a refresh between the two finds no marker.
    reports = Reports()
    lease: Final[SoftFileLease] = make_lease(marker, on_compromise=reports)
    real_lstat = os.lstat
    misses = iter([True])
    refreshes: Final[Semaphore] = Semaphore(0)

    def lstat(path: str | os.PathLike[str]) -> os.stat_result:
        refreshes.release()
        if os.fspath(path).endswith(marker.name) and next(misses, False):
            raise FileNotFoundError(ENOENT, "No such file or directory", marker.name)
        return real_lstat(path)

    with lease:
        mocker.patch("filelock._soft._lease.os.lstat", autospec=True, side_effect=lstat)
        for _ in range(4):
            assert refreshes.acquire(timeout=DEADLINE)
    assert (reports.seen, lease.compromise, next(misses, None)) == ([], None, None)


def test_lease_reports_owner_changed_when_the_marker_identity_shifts(marker: Path, mocker: MockerFixture) -> None:
    # A successor's marker has a different inode.
    reports = Reports()
    lease = make_lease(marker, on_compromise=reports)
    lease.acquire()
    token = lease.token
    real_lstat = os.lstat
    shifted = False

    def lstat(path: str | os.PathLike[str], *, dir_fd: int | None = None) -> os.stat_result | SimpleNamespace:
        st = real_lstat(path, dir_fd=dir_fd)
        if shifted and str(path).endswith(marker.name):
            return SimpleNamespace(st_dev=st.st_dev, st_ino=st.st_ino + 1)
        return st

    mocker.patch("filelock._soft._lease.os.lstat", side_effect=lstat, autospec=True)
    try:
        shifted = True
        assert reports.landed.wait(DEADLINE)
    finally:
        shifted = False
        lease.release()

    assert [(c.reason, c.token) for c in reports.seen] == [("owner-changed", token)]
