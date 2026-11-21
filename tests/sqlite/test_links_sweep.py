from __future__ import annotations

import errno
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from typing import TYPE_CHECKING, Final

import pytest
from capabilities import CAPABILITIES

pytest.importorskip("sqlite3")
import filelock._sqlite._links  # simplint: ignore[private-import-path]  # isolate sweep state
from filelock import ReadWriteLock
from tests.pid_namespace_helpers import pin_pid_namespace, pin_proc_pid_namespace
from tests.read_write_helpers import assert_read_write_lock_state
from tests.sqlite.link_support import host_digest, link_directories

if TYPE_CHECKING:
    import subprocess
    from collections.abc import Generator, Iterator
    from pathlib import Path

    from pytest_mock import MockerFixture

pytestmark: Final[list[pytest.MarkDecorator]] = [
    pytest.mark.requires_hard_links,
    pytest.mark.skipif(
        not CAPABILITIES["posix-hard-link"],
        reason="private database aliases need POSIX hard links without following symlinks",
    ),
]


def test_private_link_sweeps_the_link_of_a_killed_holder(
    shared_database: Path, posix_link_holder: subprocess.Popen[str]
) -> None:
    posix_link_holder.kill()
    posix_link_holder.wait(timeout=30)
    left: Final[list[Path]] = link_directories(shared_database)
    with ReadWriteLock(shared_database, is_singleton=False).read_lock():
        pass

    assert (len(left), link_directories(shared_database), shared_database.stat().st_nlink) == (1, [], 1)


def test_private_link_sweep_bounds_a_crowded_directory(shared_database: Path, mocker: MockerFixture) -> None:
    for number in range(300):
        (shared_database.parent / f".filelock-{number}").mkdir()
    scandir = os.scandir

    @contextmanager
    def slow_scandir(path: str | os.PathLike[str] | int) -> Generator[Iterator[os.DirEntry[str]]]:
        with scandir(path) as entries:

            def delayed_entry(entry: os.DirEntry[str]) -> os.DirEntry[str]:
                time.sleep(0.005)
                return entry

            yield map(delayed_entry, entries)

    mocker.patch("os.scandir", autospec=True, side_effect=slow_scandir)
    started = time.monotonic()
    with ReadWriteLock(shared_database, is_singleton=False).write_lock(blocking=False):
        assert shared_database.exists()
    assert time.monotonic() - started < 0.5


@pytest.mark.parametrize("link", ["lock.db", "0123456789abcdef/lock.db"])
def test_private_link_sweep_resumes_after_its_cooldown(shared_database: Path, mocker: MockerFixture, link: str) -> None:
    shared_database.touch()
    left = shared_database.parent / f".filelock-{2**31 - 1}--{host_digest(shared_database)}-x"
    (left / link).parent.mkdir(mode=0o700, parents=True)
    (left / link).hardlink_to(shared_database)
    scandir = os.scandir

    @contextmanager
    def dead_link_last(path: str | os.PathLike[str] | int) -> Generator[Iterator[os.DirEntry[str]]]:
        with scandir(path) as entries:
            yield iter(sorted(entries, key=lambda entry: entry.name == left.name))

    scans = mocker.patch("os.scandir", autospec=True, side_effect=dead_link_last)
    clock = mocker.patch("filelock._sqlite._links.time", autospec=True).monotonic
    clock.return_value = 100
    mocker.patch("filelock._sqlite._links._SWEEP_BUDGET", 0)
    lock = ReadWriteLock(shared_database, is_singleton=False)
    for _ in range(4):
        with lock.read_lock(blocking=False):
            assert (left / link).samefile(shared_database)
    assert scans.call_count == 1

    clock.return_value += 61
    with lock.read_lock(blocking=False):
        assert not left.exists()


def test_private_link_sweep_closes_abandoned_scans(shared_database: Path, mocker: MockerFixture) -> None:
    open_scans: set[int] = set()
    scandir = os.scandir

    @contextmanager
    def track_handles(path: Path) -> Generator[Iterator[os.DirEntry[str]]]:
        with scandir(path) as entries:
            open_scans.add(id(entries))
            try:
                yield entries
            finally:
                open_scans.remove(id(entries))

    mocker.patch("os.scandir", autospec=True, side_effect=track_handles)
    clock = mocker.patch("filelock._sqlite._links.time", autospec=True).monotonic
    clock.return_value = 100
    mocker.patch("filelock._sqlite._links._SWEEP_BUDGET", 0)
    for index in range(17):
        ReadWriteLock(shared_database.with_suffix(f".{index}"), is_singleton=False).close()
    assert len(open_scans) == 16
    clock.return_value += 61
    mocker.patch("filelock._sqlite._links._SWEEP_BUDGET", 1)
    for index in range(17):
        ReadWriteLock(shared_database.with_suffix(f".{index}"), is_singleton=False).close()
    assert not open_scans


def test_private_link_sweep_forgets_old_databases(shared_database: Path, mocker: MockerFixture) -> None:
    digest = host_digest(shared_database)
    mocker.patch("filelock._sqlite._links._MAX_REMEMBERED_SWEEPS", 2)
    mocker.patch("filelock._sqlite._links.LINK_SWEEPS", new=filelock._sqlite._links._LinkSweeps())
    ReadWriteLock(shared_database, is_singleton=False).close()
    left = shared_database.parent / f".filelock-{2**31 - 1}--{digest}-left"
    left.mkdir(mode=0o700)
    (left / "lock.db").hardlink_to(shared_database)
    for suffix in (".b", ".c"):
        ReadWriteLock(shared_database.with_suffix(suffix), is_singleton=False).close()
    assert (left / "lock.db").samefile(shared_database)
    ReadWriteLock(shared_database, is_singleton=False).close()
    assert not left.exists()


def test_private_link_sweep_restarts_in_a_replaced_parent(shared_database: Path, mocker: MockerFixture) -> None:
    digest = host_digest(shared_database)
    mocker.patch("filelock._sqlite._links._SWEEP_BUDGET", 0)
    ReadWriteLock(shared_database, is_singleton=False).close()
    previous = shared_database.parent.with_name("previous")
    shared_database.parent.rename(previous)
    shared_database.parent.mkdir()
    shared_database.parent.chmod(0o777)
    shared_database.hardlink_to(previous / shared_database.name)
    left = shared_database.parent / f".filelock-{2**31 - 1}--{digest}-left"
    left.mkdir(mode=0o700)
    (left / "lock.db").hardlink_to(shared_database)

    mocker.patch("filelock._sqlite._links._SWEEP_BUDGET", 1)
    ReadWriteLock(shared_database, is_singleton=False).close()
    assert not left.exists()


def test_private_link_sweep_retries_an_invalid_iterator(shared_database: Path, mocker: MockerFixture) -> None:
    digest = host_digest(shared_database)
    scandir = os.scandir

    def invalidated(entries: Iterator[os.DirEntry[str]]) -> Generator[os.DirEntry[str]]:
        yield next(entries)
        raise OSError(errno.EIO, "directory iterator invalidated")

    @contextmanager
    def interrupted(path: Path) -> Generator[Iterator[os.DirEntry[str]]]:
        with scandir(path) as entries:
            yield invalidated(entries)

    scans = mocker.patch("os.scandir", autospec=True, side_effect=interrupted)
    clock = mocker.patch("filelock._sqlite._links.time", autospec=True).monotonic
    clock.return_value = 100
    mocker.patch("filelock._sqlite._links._SWEEP_BUDGET", 0)
    ReadWriteLock(shared_database, is_singleton=False).close()
    left = shared_database.parent / f".filelock-{2**31 - 1}--{digest}-left"
    left.mkdir(mode=0o700)
    (left / "lock.db").hardlink_to(shared_database)
    clock.return_value += 61
    ReadWriteLock(shared_database, is_singleton=False).close()
    assert (left / "lock.db").samefile(shared_database)

    scans.side_effect = scandir
    clock.return_value += 61
    mocker.patch("filelock._sqlite._links._SWEEP_BUDGET", 1)
    ReadWriteLock(shared_database, is_singleton=False).close()
    assert not left.exists()


def test_private_link_sweep_skips_a_busy_iterator(shared_database: Path, mocker: MockerFixture) -> None:
    digest = host_digest(shared_database)
    scandir = os.scandir
    scanning, release = threading.Event(), threading.Event()

    @contextmanager
    def paused(path: Path) -> Generator[Iterator[os.DirEntry[str]]]:
        scanning.set()
        assert release.wait(5)
        with scandir(path) as entries:
            yield entries

    scans = mocker.patch("os.scandir", autospec=True, side_effect=paused)
    other = shared_database.with_suffix(".other")
    other.touch()
    left = other.parent / f".filelock-{2**31 - 1}--{digest}-left"
    left.mkdir(mode=0o700)
    (left / "lock.db").hardlink_to(other)
    with ThreadPoolExecutor(max_workers=1) as worker:
        first = worker.submit(ReadWriteLock, shared_database, is_singleton=False)
        try:
            assert scanning.wait(5)
            ReadWriteLock(other, is_singleton=False).close()
            assert (left / "lock.db").samefile(other)
            assert scans.call_count == 1
        finally:
            release.set()
        first.result(timeout=5).close()

    ReadWriteLock(other, is_singleton=False).close()
    assert not left.exists()


@pytest.mark.skipif(sys.platform != "linux", reason="Linux PID namespaces need procfs to identify their owners")
@pytest.mark.parametrize("procfs", [pytest.param("hidden", id="hidden"), pytest.param("foreign", id="foreign")])
@pytest.mark.parametrize("legacy", [pytest.param(False, id="current"), pytest.param(True, id="legacy")])
def test_private_link_sweep_keeps_a_holder_when_pid_namespace_is_unknown(  # pragma: linux cover
    shared_database: Path, mocker: MockerFixture, procfs: str, *, legacy: bool
) -> None:
    if procfs == "hidden":  # simplint: ignore[conditional-in-test]  # distinct procfs states
        pin_pid_namespace(mocker, PermissionError(errno.EACCES, "procfs hidden"))
    else:
        pin_proc_pid_namespace(mocker, "{pid}", "NSpid:\t{pid}\t{pid}\n")
    shared_database.touch()
    left: Final[Path] = (
        shared_database.parent / f".filelock-{2**31 - 1}--{host_digest(shared_database, legacy=legacy)}-x"
    )
    left.mkdir(mode=0o700)
    (left / "lock.db").hardlink_to(shared_database)
    with ReadWriteLock(shared_database, is_singleton=False).read_lock():
        pass

    assert (left / "lock.db").samefile(shared_database)


@pytest.mark.parametrize(
    "entry",
    [
        pytest.param("lock.db", id="flat"),
        pytest.param("inner/lock.db", id="nested"),
        pytest.param("inner/other", id="nested-without-link"),
    ],
)
def test_private_link_sweep_keeps_a_directory_named_like_a_dead_holder(shared_database: Path, entry: str) -> None:
    named: Final[Path] = shared_database.parent / f".filelock-{2**31 - 1}--{host_digest(shared_database)}-x"
    (named / entry).parent.mkdir(parents=True)
    (named / entry).write_text("keep")
    (named / "notes.txt").write_text("keep")
    ReadWriteLock(shared_database, is_singleton=False).close()

    assert [(named / name).read_text() for name in (entry, "notes.txt")] == ["keep", "keep"]


@pytest.mark.parametrize(
    ("subdirectories", "retained"),
    [pytest.param((), False, id="empty"), pytest.param(("0123456789abcdef",), True, id="empty-subdirectory")],
)
def test_private_link_sweep_removes_only_empty_directories_left_without_a_link(
    shared_database: Path, subdirectories: tuple[str, ...], retained: bool
) -> None:
    left: Final[Path] = shared_database.parent / f".filelock-{2**31 - 1}--{host_digest(shared_database)}-x"
    left.mkdir(mode=0o700)
    for name in subdirectories:
        (left / name).mkdir(mode=0o700)
    with ReadWriteLock(shared_database, is_singleton=False).read_lock():
        pass

    assert left.exists() is retained
    assert all((left / name).is_dir() for name in subdirectories)


def test_private_link_sweep_keeps_empty_directories_renamed_to_a_dead_holder(shared_database: Path) -> None:
    victim: Final[Path] = shared_database.parent / "victim-work"
    victim.mkdir()
    (victim / "incoming").mkdir()
    (victim / "outgoing").mkdir()
    (victim / "data").mkdir()
    (victim / "data" / "notes.txt").write_text("keep")
    named: Final[Path] = shared_database.parent / f".filelock-{2**31 - 1}--{host_digest(shared_database)}-x"
    victim.rename(named)

    ReadWriteLock(shared_database, is_singleton=False).close()

    assert sorted(entry.name for entry in named.iterdir()) == ["data", "incoming", "outgoing"]


def test_private_link_sweep_never_parses_a_directory_of_another_account(
    shared_database: Path, mocker: MockerFixture
) -> None:
    shared_database.touch()
    planted: Final[Path] = shared_database.parent / f".filelock-{2**31 - 1}--{host_digest(shared_database)}-x"
    planted.mkdir(mode=0o700)
    (planted / "inner").mkdir()
    (planted / "inner" / "lock.db").hardlink_to(shared_database)
    real_fstat = os.fstat

    def fstat_with_other_owner(fd: int) -> os.stat_result:
        status = real_fstat(fd)
        if status.st_ino != planted.stat().st_ino:
            return status
        fields = tuple(status)
        return os.stat_result((*fields[:4], status.st_uid + 1, *fields[5:]))

    mocker.patch("os.fstat", autospec=True, side_effect=fstat_with_other_owner)
    mocker.patch("os.kill", autospec=True, side_effect=AssertionError("PID probed"))
    ReadWriteLock(shared_database, is_singleton=False).close()

    assert ((planted / "inner" / "lock.db").exists(), shared_database.stat().st_nlink) == (True, 2)


def test_private_link_sweep_skips_a_symlink_under_a_dead_holder_name(shared_database: Path) -> None:
    # The symlink leads to the database's own directory.
    shared_database.touch()
    named: Final[Path] = shared_database.parent / f".filelock-{2**31 - 1}--{host_digest(shared_database)}-x"
    named.mkdir()
    (named / "0123456789abcdef").symlink_to(shared_database.parent)
    ReadWriteLock(shared_database, is_singleton=False).close()

    assert (shared_database.exists(), (named / "0123456789abcdef").is_symlink()) == (True, True)


def test_private_link_sweep_keeps_a_link_under_an_unrelated_directory(shared_database: Path) -> None:
    shared_database.touch()
    named: Final[Path] = shared_database.parent / f".filelock-{2**31 - 1}--{host_digest(shared_database)}-x"
    (named / "project").mkdir(parents=True)
    (named / "project" / "lock.db").hardlink_to(shared_database)
    ReadWriteLock(shared_database, is_singleton=False).close()

    assert ((named / "project" / "lock.db").exists(), shared_database.stat().st_nlink) == (True, 2)


@pytest.mark.parametrize("depth", [1, 2])
def test_private_link_sweep_skips_a_symlink_to_a_live_holder(
    shared_database: Path, posix_link_holder: subprocess.Popen[str], depth: int
) -> None:
    (held,) = link_directories(shared_database)
    (outer,) = list(held.iterdir())
    (inner,) = list(outer.iterdir())
    named: Final[Path] = shared_database.parent / f".filelock-{2**31 - 1}--{host_digest(shared_database)}-x"
    planted: Final[Path] = named / "/".join(["0123456789abcdef"] * depth)
    planted.parent.mkdir(parents=True, exist_ok=True)
    planted.symlink_to((outer, inner)[depth - 1])
    ReadWriteLock(shared_database, is_singleton=False).close()

    assert ((inner / "lock.db").exists(), planted.is_symlink(), posix_link_holder.poll()) == (True, True, None)


@pytest.mark.usefixtures("posix_link_holder")
def test_private_link_sweep_keeps_the_link_of_a_live_holder(shared_database: Path) -> None:
    held: Final[list[Path]] = link_directories(shared_database)
    with ReadWriteLock(shared_database, is_singleton=False).read_lock():
        pass
    assert (len(held), link_directories(shared_database)) == (1, held)


def test_private_link_sweep_keeps_a_directory_it_did_not_name(shared_database: Path) -> None:
    foreign: Final[Path] = shared_database.parent / ".filelock-not-ours"
    foreign.mkdir()
    with ReadWriteLock(shared_database, is_singleton=False).read_lock():
        pass
    assert foreign.is_dir()


@pytest.mark.parametrize(
    ("pid_text", "start_text"),
    [
        pytest.param("²", "1", id="non-ascii-pid"),
        pytest.param("\uff19" * 9, "1", id="parsed-non-ascii-pid"),
        pytest.param(str(2**31), "1", id="out-of-range-pid"),
        pytest.param("0", "1", id="zero-pid"),
        pytest.param("1", "²", id="non-ascii-start"),
    ],
)
def test_private_link_sweep_survives_a_malformed_link_name(
    shared_database: Path, mocker: MockerFixture, pid_text: str, start_text: str
) -> None:
    malformed: Final[Path] = (
        shared_database.parent / f".filelock-{pid_text}-{start_text}-{host_digest(shared_database)}-x"
    )
    malformed.mkdir()
    mocker.patch("os.kill", autospec=True, side_effect=AssertionError("invalid PID probed"))
    with ReadWriteLock(shared_database, is_singleton=False).read_lock():
        assert_read_write_lock_state(str(shared_database), "write", available=False)
    assert malformed.is_dir()
