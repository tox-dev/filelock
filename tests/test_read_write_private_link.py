from __future__ import annotations

import asyncio
import errno
import hashlib
import os
import shutil
import stat
import subprocess  # ruff:ignore[suspicious-subprocess-import]  # runs this test's own interpreter
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Final, Literal
from urllib.parse import unquote, urlsplit

import pytest
from capabilities import CAPABILITIES

pytest.importorskip("sqlite3")

import sqlite3

import filelock._read_write  # simplint: ignore[private-import-path]  # isolate sweep state
from filelock import AsyncReadWriteLock, ReadWriteLock
from filelock._identity import host_name
from tests.capability_marks import NEEDS_FILE_PERMISSIONS
from tests.pid_namespace_helpers import pin_pid_namespace, pin_proc_pid_namespace
from tests.read_write_helpers import assert_read_write_lock_state

if TYPE_CHECKING:
    from collections.abc import Callable, Generator, Iterator

    from pytest_mock import MockerFixture

pytestmark: Final[list[pytest.MarkDecorator]] = [
    pytest.mark.requires_hard_links,
    pytest.mark.skipif(
        not CAPABILITIES["posix-hard-link"],
        reason="private database aliases need POSIX hard links without following symlinks",
    ),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", [pytest.param("read", id="read"), pytest.param("write", id="write")])
async def test_private_link_connects_through_the_validated_inode(
    database: Path, mocker: MockerFixture, mode: Literal["read", "write"]
) -> None:
    connect = mocker.spy(sqlite3, "connect")
    lock: Final[AsyncReadWriteLock] = AsyncReadWriteLock(database, is_singleton=False)
    try:
        async with getattr(lock, f"{mode}_lock")():
            opened: Final[Path] = Path(unquote(urlsplit(connect.call_args.args[0]).path))
            link_directory: Final[Path] = opened.parents[2]
            assert (
                (connect.call_args.kwargs["uri"], urlsplit(connect.call_args.args[0]).query),
                opened.name,
                link_directory.parent,
                stat.S_IMODE(link_directory.stat().st_mode),
                await asyncio.to_thread(opened.samefile, database),
            ) == ((True, "mode=rw"), "lock.db", database.parent, 0o700, True)
            await asyncio.to_thread(assert_read_write_lock_state, str(database), "write", available=False)
    finally:
        await lock.close()


def test_private_link_opens_when_its_directory_needs_uri_quoting(database: Path) -> None:
    parent = database.parent.with_name("shared? #%")
    database.parent.rename(parent)
    database = parent / database.name
    with ReadWriteLock(database, is_singleton=False).write_lock():
        assert_read_write_lock_state(str(database), "write", available=False)


@pytest.mark.parametrize("mode", [pytest.param("read", id="read"), pytest.param("write", id="write")])
def test_private_link_preserves_contention(database: Path, mode: Literal["read", "write"]) -> None:
    lock: Final[ReadWriteLock] = ReadWriteLock(database, is_singleton=False)
    with getattr(lock, f"{mode}_lock")():
        assert_read_write_lock_state(str(database), "write", available=False)


@pytest.mark.parametrize(
    "boundary", [pytest.param("tempfile.mkdtemp", id="mkdtemp"), pytest.param("os.link", id="link")]
)
def test_private_link_failure_preserves_a_peer_lock(database: Path, mocker: MockerFixture, boundary: str) -> None:
    with ReadWriteLock(database, is_singleton=False).read_lock():
        mocker.patch(boundary, autospec=True, side_effect=PermissionError(errno.EACCES, "read-only directory"))
        with pytest.raises(PermissionError, match="read-only directory"):
            ReadWriteLock(database, is_singleton=False)
        assert_read_write_lock_state(str(database), "write", available=False)
    assert list(database.parent.glob(".filelock-*")) == []


def test_private_link_removed_before_sqlite_open_cannot_grant_a_second_writer(
    database: Path, mocker: MockerFixture, created_directories: list[Path]
) -> None:
    with ReadWriteLock(database, is_singleton=False).write_lock():
        connect = sqlite3.connect

        def remove_then_connect(
            target: str,
            *,
            check_same_thread: bool,
            factory: type[sqlite3.Connection],
            cached_statements: int,
            timeout: float,
            uri: bool = False,
        ) -> sqlite3.Connection:
            next(created_directories[-1].glob("*/*/lock.db")).unlink()
            return connect(
                target,
                check_same_thread=check_same_thread,
                factory=factory,
                cached_statements=cached_statements,
                timeout=timeout,
                uri=uri,
            )

        patched = mocker.patch("sqlite3.connect", autospec=True, side_effect=remove_then_connect)
        with pytest.raises(sqlite3.OperationalError, match="unable to open database file"):
            ReadWriteLock(database, is_singleton=False)
        mocker.stop(patched)
        assert_read_write_lock_state(str(database), "write", available=False)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", [pytest.param("read", id="read"), pytest.param("write", id="write")])
async def test_private_link_preserves_async_contention(database: Path, mode: Literal["read", "write"]) -> None:
    lock: Final[AsyncReadWriteLock] = AsyncReadWriteLock(database, is_singleton=False)
    async with getattr(lock, f"{mode}_lock")():
        assert_read_write_lock_state(str(database), "write", available=False)
    await lock.close()


def test_private_link_is_removed_after_release(database: Path, created_directories: list[Path]) -> None:
    lock: Final[ReadWriteLock] = ReadWriteLock(database, is_singleton=False)
    lock.acquire_read()
    assert [directory.exists() for directory in created_directories] == [False, True]
    lock.release()
    assert [directory.exists() for directory in created_directories] == [False, False]


def test_private_link_sits_beside_the_database(database: Path, created_directories: list[Path]) -> None:
    # A hard link cannot cross filesystems, so an alias in TMPDIR fails for a database stored anywhere else.
    ReadWriteLock(database, is_singleton=False).close()
    assert [directory.parent for directory in created_directories] == [database.parent]


def test_private_link_release_survives_a_removed_alias(database: Path, created_directories: list[Path]) -> None:
    lock: Final[ReadWriteLock] = ReadWriteLock(database, is_singleton=False)
    lock.acquire_read()
    shutil.rmtree(created_directories[-1])
    lock.release()

    with ReadWriteLock(database, is_singleton=False).write_lock(blocking=False):
        assert_read_write_lock_state(str(database), "write", available=False)


@pytest.mark.parametrize(
    "boundary",
    [pytest.param("os.link", id="link"), pytest.param("sqlite3.connect", id="connect")],
)
def test_private_link_is_removed_after_failure(
    database: Path, mocker: MockerFixture, boundary: str, created_directories: list[Path]
) -> None:
    mocker.patch(boundary, autospec=True, side_effect=OSError("cannot open database"))
    with pytest.raises(OSError, match="cannot open database"):
        ReadWriteLock(database, is_singleton=False)
    assert [directory.exists() for directory in created_directories] == [False]


@pytest.mark.parametrize(
    "make_replacement",
    [
        pytest.param(Path.touch, id="file"),
        pytest.param(lambda path: path.symlink_to(path.parent / "victim.db"), id="symlink"),
    ],
)
def test_private_link_rejects_replaced_database(
    database: Path,
    tmp_path: Path,
    mocker: MockerFixture,
    make_replacement: Callable[[Path], object],
    created_directories: list[Path],
) -> None:
    replacement: Final[Path] = tmp_path / "replacement.db"
    make_replacement(replacement)
    link = os.link

    def replace_and_link(source: str, destination: Path, *, follow_symlinks: bool) -> None:
        replacement.replace(source)
        link(source, destination, follow_symlinks=follow_symlinks)

    mocker.patch("os.link", autospec=True, side_effect=replace_and_link)
    with pytest.raises(OSError, match="database changed"):
        ReadWriteLock(database, is_singleton=False)
    assert ([directory.exists() for directory in created_directories], (tmp_path / "victim.db").exists()) == (
        [False],
        False,
    )


def test_private_link_refuses_a_directory_swapped_in_before_it_opens(
    database: Path, tmp_path: Path, mocker: MockerFixture
) -> None:
    swapped_in: Final[list[Path]] = []
    mkdtemp = tempfile.mkdtemp

    def create_then_swap(*, prefix: str, dir: Path) -> str:  # ruff:ignore[builtin-argument-shadowing]  # mirrors tempfile.mkdtemp
        directory: Final[Path] = Path(mkdtemp(prefix=prefix, dir=dir))
        directory.rename(tmp_path / "aside")
        directory.mkdir(mode=0o755)
        (directory / "notes.txt").write_text("keep")
        swapped_in.append(directory)
        return str(directory)

    mocker.patch("tempfile.mkdtemp", autospec=True, side_effect=create_then_swap)
    with pytest.raises(OSError, match="replaced while opening"):
        ReadWriteLock(database, is_singleton=False)
    assert [(directory / "notes.txt").read_text() for directory in swapped_in] == ["keep"]


def test_private_link_repairs_inherited_permissions_on_the_nested_directory(
    database: Path, mocker: MockerFixture
) -> None:
    mkdir = os.mkdir

    def inherit_permissions(path: str, mode: int = 0o777, *, dir_fd: int | None = None) -> None:
        mkdir(path, mode, dir_fd=dir_fd)
        if dir_fd is not None:
            os.chmod(path, stat.S_IRWXU | stat.S_IRGRP, dir_fd=dir_fd)

    mocker.patch("os.mkdir", autospec=True, side_effect=inherit_permissions)
    with ReadWriteLock(database, is_singleton=False).read_lock():
        directory = next(database.parent.glob(".filelock-*/*"))
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700

    mocker.patch("os.fchmod", autospec=True)
    with pytest.raises(PermissionError, match="inherited ACL"):
        ReadWriteLock(database, is_singleton=False)
    assert list(database.parent.glob(".filelock-*")) == []


def test_private_link_closes_a_mode_an_inherited_acl_opened(database: Path, loosened_directories: list[Path]) -> None:
    lock: Final[ReadWriteLock] = ReadWriteLock(database, is_singleton=False)
    with lock.read_lock():
        assert [stat.S_IMODE(directory.stat().st_mode) for directory in loosened_directories[1:]] == [0o700]


@pytest.mark.parametrize(
    "fchmod_error",
    [
        pytest.param(None, id="ignored"),
        pytest.param(PermissionError(errno.EPERM, "Operation not permitted"), id="refused"),
    ],
)
def test_private_link_refuses_a_directory_chmod_cannot_close(
    database: Path, loosened_directories: list[Path], mocker: MockerFixture, fchmod_error: PermissionError | None
) -> None:
    # ZFS with aclmode=restricted refuses a chmod on a directory carrying an inherited ACL.
    mocker.patch("os.fchmod", autospec=True, side_effect=fchmod_error)
    with pytest.raises(OSError, match="writable by its owner only"):
        ReadWriteLock(database, is_singleton=False)
    assert len(loosened_directories) == 1


def test_private_link_opens_nothing_through_a_swapped_link_directory(
    database: Path, tmp_path: Path, mocker: MockerFixture, created_directories: list[Path]
) -> None:
    # Whoever can rename in the lock directory may swap the link directory for one of its own before SQLite opens it.
    target: Final[Path] = tmp_path / "target.db"
    connect = sqlite3.connect

    def swap_then_connect(
        database: str,
        *,
        check_same_thread: bool,
        factory: type[sqlite3.Connection],
        cached_statements: int,
        timeout: float,
        uri: bool = False,
    ) -> sqlite3.Connection:
        created_directories[-1].rename(tmp_path / "aside")
        created_directories[-1].mkdir()
        (created_directories[-1] / "lock.db").symlink_to(target)
        return connect(
            database,
            check_same_thread=check_same_thread,
            factory=factory,
            cached_statements=cached_statements,
            timeout=timeout,
            uri=uri,
        )

    mocker.patch("sqlite3.connect", autospec=True, side_effect=swap_then_connect)
    with pytest.raises(sqlite3.OperationalError, match="unable to open database file"):
        ReadWriteLock(database, is_singleton=False)
    assert (target.exists(), (created_directories[-1] / "lock.db").is_symlink()) == (False, True)


def test_private_link_refuses_a_watched_swapped_directory(
    database: Path, tmp_path: Path, mocker: MockerFixture
) -> None:
    target = tmp_path / "target.db"
    watched = database.parent / "results"
    watched.mkdir(mode=0o755)
    adopted: list[Path] = []
    mkdtemp = tempfile.mkdtemp
    connect = sqlite3.connect

    def create_then_swap(*, prefix: str, dir: Path) -> str:  # ruff:ignore[builtin-argument-shadowing]  # mirrors mkdtemp
        directory = Path(mkdtemp(prefix=prefix, dir=dir))
        watched.rename(directory)
        adopted.append(directory)
        return str(directory)

    def swap_then_connect(
        database: str,
        *,
        check_same_thread: bool,
        factory: type[sqlite3.Connection],
        cached_statements: int,
        timeout: float,
        uri: bool,
    ) -> sqlite3.Connection:
        reported = [entry.name for entry in adopted[-1].iterdir()]
        adopted[-1].rename(watched)
        for name in reported:
            (adopted[-1] / name).mkdir(parents=True)
            (adopted[-1] / name / "lock.db").symlink_to(target)
        return connect(
            database,
            check_same_thread=check_same_thread,
            factory=factory,
            cached_statements=cached_statements,
            timeout=timeout,
            uri=uri,
        )

    mocker.patch("tempfile.mkdtemp", autospec=True, side_effect=create_then_swap)
    mocker.patch("sqlite3.connect", autospec=True, side_effect=swap_then_connect)
    with pytest.raises(sqlite3.OperationalError, match="unable to open database file"):
        ReadWriteLock(database, is_singleton=False)
    assert target.exists() is False


def test_private_link_release_keeps_a_directory_renamed_onto_the_link(database: Path, tmp_path: Path) -> None:
    other: Final[Path] = database.parent / "project"
    other.mkdir()
    (other / "notes.txt").write_text("keep")
    with ReadWriteLock(database, is_singleton=False).write_lock():
        (link_directory,) = _link_directories(database)
        link_directory.rename(tmp_path / "aside")
        other.rename(link_directory)

    assert (link_directory / "notes.txt").read_text() == "keep"


def _link_directories(database: Path) -> list[Path]:
    return sorted(path for path in database.parent.iterdir() if path.name.startswith(".filelock-"))


def test_private_link_sweeps_the_link_of_a_killed_holder(database: Path, holder: subprocess.Popen[str]) -> None:
    holder.kill()
    holder.wait(timeout=_PROCESS_DEADLINE)
    left: Final[list[Path]] = _link_directories(database)
    with ReadWriteLock(database, is_singleton=False).read_lock():
        pass

    assert (len(left), _link_directories(database), database.stat().st_nlink) == (1, [], 1)


def test_private_link_sweep_bounds_a_crowded_directory(database: Path, mocker: MockerFixture) -> None:
    for number in range(300):
        (database.parent / f".filelock-{number}").mkdir()
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
    with ReadWriteLock(database, is_singleton=False).write_lock(blocking=False):
        assert database.exists()
    assert time.monotonic() - started < 0.5


@pytest.mark.parametrize("link", ["lock.db", "0123456789abcdef/lock.db"])
def test_private_link_sweep_resumes_after_its_cooldown(database: Path, mocker: MockerFixture, link: str) -> None:
    database.touch()
    left = database.parent / f".filelock-{2**31 - 1}--{_host_digest(database)}-x"
    (left / link).parent.mkdir(mode=0o700, parents=True)
    (left / link).hardlink_to(database)
    scandir = os.scandir

    @contextmanager
    def dead_link_last(path: str | os.PathLike[str] | int) -> Generator[Iterator[os.DirEntry[str]]]:
        with scandir(path) as entries:
            yield iter(sorted(entries, key=lambda entry: entry.name == left.name))

    scans = mocker.patch("os.scandir", autospec=True, side_effect=dead_link_last)
    clock = mocker.patch("filelock._read_write.time", autospec=True).monotonic
    clock.return_value = 100
    mocker.patch("filelock._read_write._MAX_TRANSITION_WAIT", 0)
    lock = ReadWriteLock(database, is_singleton=False)
    for _ in range(4):
        with lock.read_lock(blocking=False):
            assert (left / link).samefile(database)
    assert scans.call_count == 1

    clock.return_value += 61
    with lock.read_lock(blocking=False):
        assert not left.exists()


def _host_digest(database: Path, *, legacy: bool = False) -> str:
    if legacy:  # pragma: linux cover
        return hashlib.sha256(host_name().encode()).hexdigest()[:16]
    # Taken from a link of another database, so the sweep for this one has not run yet.
    seed: Final[Path] = database.parent.with_name("seed")
    seed.mkdir(mode=0o777)
    seed.chmod(0o777)
    with ReadWriteLock(seed / "lock.db", is_singleton=False).read_lock():
        return next(seed.glob(".filelock-*")).name.rsplit("-", 2)[1]


def test_private_link_sweep_closes_abandoned_scans(database: Path, mocker: MockerFixture) -> None:
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
    clock = mocker.patch("filelock._read_write.time", autospec=True).monotonic
    clock.return_value = 100
    mocker.patch("filelock._read_write._MAX_TRANSITION_WAIT", 0)
    for index in range(17):
        ReadWriteLock(database.with_suffix(f".{index}"), is_singleton=False).close()
    assert len(open_scans) == 16
    clock.return_value += 61
    mocker.patch("filelock._read_write._MAX_TRANSITION_WAIT", 1)
    for index in range(17):
        ReadWriteLock(database.with_suffix(f".{index}"), is_singleton=False).close()
    assert not open_scans


def test_private_link_sweep_forgets_old_databases(database: Path, mocker: MockerFixture) -> None:
    digest = _host_digest(database)
    mocker.patch("filelock._read_write._MAX_REMEMBERED_SWEEPS", 2)
    mocker.patch("filelock._read_write._LINK_SWEEPS", new=filelock._read_write._LinkSweeps())
    ReadWriteLock(database, is_singleton=False).close()
    left = database.parent / f".filelock-{2**31 - 1}--{digest}-left"
    left.mkdir(mode=0o700)
    (left / "lock.db").hardlink_to(database)
    for suffix in (".b", ".c"):
        ReadWriteLock(database.with_suffix(suffix), is_singleton=False).close()
    assert (left / "lock.db").samefile(database)
    ReadWriteLock(database, is_singleton=False).close()
    assert not left.exists()


def test_private_link_sweep_restarts_in_a_replaced_parent(database: Path, mocker: MockerFixture) -> None:
    digest = _host_digest(database)
    mocker.patch("filelock._read_write._MAX_TRANSITION_WAIT", 0)
    ReadWriteLock(database, is_singleton=False).close()
    previous = database.parent.with_name("previous")
    database.parent.rename(previous)
    database.parent.mkdir()
    database.parent.chmod(0o777)
    database.hardlink_to(previous / database.name)
    left = database.parent / f".filelock-{2**31 - 1}--{digest}-left"
    left.mkdir(mode=0o700)
    (left / "lock.db").hardlink_to(database)

    mocker.patch("filelock._read_write._MAX_TRANSITION_WAIT", 1)
    ReadWriteLock(database, is_singleton=False).close()
    assert not left.exists()


def test_private_link_sweep_retries_an_invalid_iterator(database: Path, mocker: MockerFixture) -> None:
    digest = _host_digest(database)
    scandir = os.scandir

    def invalidated(entries: Iterator[os.DirEntry[str]]) -> Generator[os.DirEntry[str]]:
        yield next(entries)
        raise OSError(errno.EIO, "directory iterator invalidated")

    @contextmanager
    def interrupted(path: Path) -> Generator[Iterator[os.DirEntry[str]]]:
        with scandir(path) as entries:
            yield invalidated(entries)

    scans = mocker.patch("os.scandir", autospec=True, side_effect=interrupted)
    clock = mocker.patch("filelock._read_write.time", autospec=True).monotonic
    clock.return_value = 100
    mocker.patch("filelock._read_write._MAX_TRANSITION_WAIT", 0)
    ReadWriteLock(database, is_singleton=False).close()
    left = database.parent / f".filelock-{2**31 - 1}--{digest}-left"
    left.mkdir(mode=0o700)
    (left / "lock.db").hardlink_to(database)
    clock.return_value += 61
    ReadWriteLock(database, is_singleton=False).close()
    assert (left / "lock.db").samefile(database)

    scans.side_effect = scandir
    clock.return_value += 61
    mocker.patch("filelock._read_write._MAX_TRANSITION_WAIT", 1)
    ReadWriteLock(database, is_singleton=False).close()
    assert not left.exists()


def test_private_link_sweep_skips_a_busy_iterator(database: Path, mocker: MockerFixture) -> None:
    digest = _host_digest(database)
    scandir = os.scandir
    scanning, release = threading.Event(), threading.Event()

    @contextmanager
    def paused(path: Path) -> Generator[Iterator[os.DirEntry[str]]]:
        scanning.set()
        assert release.wait(5)
        with scandir(path) as entries:
            yield entries

    scans = mocker.patch("os.scandir", autospec=True, side_effect=paused)
    other = database.with_suffix(".other")
    other.touch()
    left = other.parent / f".filelock-{2**31 - 1}--{digest}-left"
    left.mkdir(mode=0o700)
    (left / "lock.db").hardlink_to(other)
    with ThreadPoolExecutor(max_workers=1) as worker:
        first = worker.submit(ReadWriteLock, database, is_singleton=False)
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
    database: Path, mocker: MockerFixture, procfs: str, *, legacy: bool
) -> None:
    if procfs == "hidden":  # simplint: ignore[conditional-in-test]  # distinct procfs states
        pin_pid_namespace(mocker, PermissionError(errno.EACCES, "procfs hidden"))
    else:
        pin_proc_pid_namespace(mocker, "{pid}", "NSpid:\t{pid}\t{pid}\n")
    database.touch()
    left: Final[Path] = database.parent / f".filelock-{2**31 - 1}--{_host_digest(database, legacy=legacy)}-x"
    left.mkdir(mode=0o700)
    (left / "lock.db").hardlink_to(database)
    with ReadWriteLock(database, is_singleton=False).read_lock():
        pass

    assert (left / "lock.db").samefile(database)


@pytest.mark.parametrize(
    "entry",
    [
        pytest.param("lock.db", id="flat"),
        pytest.param("inner/lock.db", id="nested"),
        pytest.param("inner/other", id="nested-without-link"),
    ],
)
def test_private_link_sweep_keeps_a_directory_named_like_a_dead_holder(database: Path, entry: str) -> None:
    named: Final[Path] = database.parent / f".filelock-{2**31 - 1}--{_host_digest(database)}-x"
    (named / entry).parent.mkdir(parents=True)
    (named / entry).write_text("keep")
    (named / "notes.txt").write_text("keep")
    ReadWriteLock(database, is_singleton=False).close()

    assert [(named / name).read_text() for name in (entry, "notes.txt")] == ["keep", "keep"]


@pytest.mark.parametrize(
    "subdirectories",
    [pytest.param((), id="empty"), pytest.param(("0123456789abcdef",), id="empty-subdirectory")],
)
def test_private_link_sweep_keeps_a_directory_left_without_a_link(
    database: Path, subdirectories: tuple[str, ...]
) -> None:
    left: Final[Path] = database.parent / f".filelock-{2**31 - 1}--{_host_digest(database)}-x"
    left.mkdir(mode=0o700)
    for name in subdirectories:
        (left / name).mkdir(mode=0o700)
    with ReadWriteLock(database, is_singleton=False).read_lock():
        pass

    assert _link_directories(database) == [left]
    assert all((left / name).is_dir() for name in subdirectories)


def test_private_link_sweep_keeps_empty_directories_renamed_to_a_dead_holder(database: Path) -> None:
    victim: Final[Path] = database.parent / "victim-work"
    victim.mkdir()
    (victim / "incoming").mkdir()
    (victim / "outgoing").mkdir()
    (victim / "data").mkdir()
    (victim / "data" / "notes.txt").write_text("keep")
    named: Final[Path] = database.parent / f".filelock-{2**31 - 1}--{_host_digest(database)}-x"
    victim.rename(named)

    ReadWriteLock(database, is_singleton=False).close()

    assert sorted(entry.name for entry in named.iterdir()) == ["data", "incoming", "outgoing"]


def test_private_link_sweep_never_parses_a_directory_of_another_account(database: Path, mocker: MockerFixture) -> None:
    database.touch()
    planted: Final[Path] = database.parent / f".filelock-{2**31 - 1}--{_host_digest(database)}-x"
    planted.mkdir(mode=0o700)
    (planted / "inner").mkdir()
    (planted / "inner" / "lock.db").hardlink_to(database)
    real_fstat = os.fstat

    def fstat_with_other_owner(fd: int) -> os.stat_result:
        status = real_fstat(fd)
        if status.st_ino != planted.stat().st_ino:
            return status
        fields = tuple(status)
        return os.stat_result((*fields[:4], status.st_uid + 1, *fields[5:]))

    mocker.patch("os.fstat", autospec=True, side_effect=fstat_with_other_owner)
    mocker.patch("os.kill", autospec=True, side_effect=AssertionError("PID probed"))
    ReadWriteLock(database, is_singleton=False).close()

    assert ((planted / "inner" / "lock.db").exists(), database.stat().st_nlink) == (True, 2)


def test_private_link_sweep_skips_a_symlink_under_a_dead_holder_name(database: Path) -> None:
    # The symlink leads to the database's own directory.
    database.touch()
    named: Final[Path] = database.parent / f".filelock-{2**31 - 1}--{_host_digest(database)}-x"
    named.mkdir()
    (named / "0123456789abcdef").symlink_to(database.parent)
    ReadWriteLock(database, is_singleton=False).close()

    assert (database.exists(), (named / "0123456789abcdef").is_symlink()) == (True, True)


def test_private_link_sweep_keeps_a_link_under_an_unrelated_directory(database: Path) -> None:
    database.touch()
    named: Final[Path] = database.parent / f".filelock-{2**31 - 1}--{_host_digest(database)}-x"
    (named / "project").mkdir(parents=True)
    (named / "project" / "lock.db").hardlink_to(database)
    ReadWriteLock(database, is_singleton=False).close()

    assert ((named / "project" / "lock.db").exists(), database.stat().st_nlink) == (True, 2)


@pytest.mark.parametrize("depth", [1, 2])
def test_private_link_sweep_skips_a_symlink_to_a_live_holder(
    database: Path, holder: subprocess.Popen[str], depth: int
) -> None:
    (held,) = _link_directories(database)
    (outer,) = list(held.iterdir())
    (inner,) = list(outer.iterdir())
    named: Final[Path] = database.parent / f".filelock-{2**31 - 1}--{_host_digest(database)}-x"
    planted: Final[Path] = named / "/".join(["0123456789abcdef"] * depth)
    planted.parent.mkdir(parents=True, exist_ok=True)
    planted.symlink_to((outer, inner)[depth - 1])
    ReadWriteLock(database, is_singleton=False).close()

    assert ((inner / "lock.db").exists(), planted.is_symlink(), holder.poll()) == (True, True, None)


@pytest.mark.usefixtures("holder")
def test_private_link_sweep_keeps_the_link_of_a_live_holder(database: Path) -> None:
    held: Final[list[Path]] = _link_directories(database)
    with ReadWriteLock(database, is_singleton=False).read_lock():
        pass
    assert (len(held), _link_directories(database)) == (1, held)


def test_private_link_sweep_keeps_a_directory_it_did_not_name(database: Path) -> None:
    foreign: Final[Path] = database.parent / ".filelock-not-ours"
    foreign.mkdir()
    with ReadWriteLock(database, is_singleton=False).read_lock():
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
    database: Path, mocker: MockerFixture, pid_text: str, start_text: str
) -> None:
    malformed: Final[Path] = database.parent / f".filelock-{pid_text}-{start_text}-{_host_digest(database)}-x"
    malformed.mkdir()
    mocker.patch("os.kill", autospec=True, side_effect=AssertionError("invalid PID probed"))
    with ReadWriteLock(database, is_singleton=False).read_lock():
        assert_read_write_lock_state(str(database), "write", available=False)
    assert malformed.is_dir()


@NEEDS_FILE_PERMISSIONS  # pragma: needs file-permissions
def test_private_link_in_a_directory_it_cannot_list(database: Path) -> None:
    database.parent.chmod(0o333)
    try:
        with ReadWriteLock(database, is_singleton=False).read_lock():
            assert_read_write_lock_state(str(database), "write", available=False)
    finally:
        database.parent.chmod(0o777)


_PROCESS_DEADLINE: Final[int] = 30


@pytest.fixture
def holder(database: Path) -> Generator[subprocess.Popen[str]]:
    script: Final[str] = (
        "import sys, time\n"
        "from filelock import ReadWriteLock\n"
        "lock = ReadWriteLock(sys.argv[1], is_singleton=False)\n"
        "lock.acquire_read()\n"
        "print('held', flush=True)\n"
        "time.sleep(600)\n"
    )
    with subprocess.Popen([sys.executable, "-c", script, str(database)], stdout=subprocess.PIPE, text=True) as holder:
        try:
            assert holder.stdout is not None
            assert holder.stdout.readline() == "held\n"
            yield holder
        finally:
            holder.kill()
            holder.wait(timeout=_PROCESS_DEADLINE)


@pytest.fixture
def database(tmp_path: Path) -> Path:
    # Others may create names in this directory, so SQLite connects through a private link instead of the name.
    shared: Final[Path] = tmp_path / "shared"
    shared.mkdir()
    shared.chmod(0o777)
    return shared / "lock.db"


@pytest.fixture
def created_directories(mocker: MockerFixture) -> list[Path]:
    directories: Final[list[Path]] = []
    mkdtemp = tempfile.mkdtemp

    def create_directory(*, prefix: str, dir: Path) -> str:  # ruff:ignore[builtin-argument-shadowing]  # mirrors tempfile.mkdtemp
        directory: Final[str] = mkdtemp(prefix=prefix, dir=dir)
        directories.append(Path(directory))
        return directory

    mocker.patch("tempfile.mkdtemp", autospec=True, side_effect=create_directory)
    return directories


@pytest.fixture
def loosened_directories(mocker: MockerFixture) -> list[Path]:
    directories: Final[list[Path]] = []
    mkdtemp = tempfile.mkdtemp

    def create_with_group_access(*, prefix: str, dir: Path) -> str:  # ruff:ignore[builtin-argument-shadowing]  # mirrors tempfile.mkdtemp
        # Stands in for an inheritable ACL entry that grants the group access to every new directory.
        directory: Final[Path] = Path(mkdtemp(prefix=prefix, dir=dir))
        directory.chmod(0o770)
        directories.append(directory)
        return str(directory)

    mocker.patch("tempfile.mkdtemp", autospec=True, side_effect=create_with_group_access)
    return directories
