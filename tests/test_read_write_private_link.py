from __future__ import annotations

import asyncio
import errno
import os
import shutil
import stat
import subprocess  # ruff:ignore[suspicious-subprocess-import]  # runs this test's own interpreter
import sys
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Final, Literal

import pytest
from capabilities import CAPABILITIES

pytest.importorskip("sqlite3")

import sqlite3

from filelock import AsyncReadWriteLock, ReadWriteLock
from tests.capability_marks import NEEDS_FILE_PERMISSIONS
from tests.read_write_helpers import assert_read_write_lock_state

if TYPE_CHECKING:
    from collections.abc import Generator

    from pytest_mock import MockerFixture

pytestmark: Final = [
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
    connect: Final = mocker.spy(sqlite3, "connect")
    lock: Final = AsyncReadWriteLock(database, is_singleton=False)
    try:
        async with (lock.read_lock if mode == "read" else lock.write_lock)():
            opened: Final = Path(connect.call_args.args[0])
            link_directory: Final = opened.parent.parent
            assert (
                opened.name,
                link_directory.parent,
                stat.S_IMODE(link_directory.stat().st_mode),
                await asyncio.to_thread(opened.samefile, database),
            ) == ("lock.db", database.parent, 0o700, True)
            await asyncio.to_thread(assert_read_write_lock_state, str(database), "write", available=False)
    finally:
        await lock.close()


@pytest.mark.parametrize("mode", [pytest.param("read", id="read"), pytest.param("write", id="write")])
def test_private_link_preserves_contention(database: Path, mode: Literal["read", "write"]) -> None:
    lock: Final = ReadWriteLock(database, is_singleton=False)
    with (lock.read_lock if mode == "read" else lock.write_lock)():
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


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", [pytest.param("read", id="read"), pytest.param("write", id="write")])
async def test_private_link_preserves_async_contention(database: Path, mode: Literal["read", "write"]) -> None:
    lock: Final = AsyncReadWriteLock(database, is_singleton=False)
    async with (lock.read_lock if mode == "read" else lock.write_lock)():
        assert_read_write_lock_state(str(database), "write", available=False)
    await lock.close()


def test_private_link_is_removed_after_release(database: Path, created_directories: list[Path]) -> None:
    lock: Final = ReadWriteLock(database, is_singleton=False)
    lock.acquire_read()
    assert [directory.exists() for directory in created_directories] == [False, True]
    lock.release()
    assert [directory.exists() for directory in created_directories] == [False, False]


def test_private_link_sits_beside_the_database(database: Path, created_directories: list[Path]) -> None:
    # A hard link cannot cross filesystems, so an alias in TMPDIR fails for a database stored anywhere else.
    ReadWriteLock(database, is_singleton=False).close()
    assert [directory.parent for directory in created_directories] == [database.parent]


def test_private_link_release_survives_a_removed_alias(database: Path, created_directories: list[Path]) -> None:
    lock: Final = ReadWriteLock(database, is_singleton=False)
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


@pytest.mark.parametrize("replacement_kind", [pytest.param("file", id="file"), pytest.param("symlink", id="symlink")])
def test_private_link_rejects_replaced_database(
    database: Path, tmp_path: Path, mocker: MockerFixture, replacement_kind: str, created_directories: list[Path]
) -> None:
    replacement: Final = tmp_path / "replacement.db"
    if replacement_kind == "file":
        replacement.touch()
    else:
        replacement.symlink_to(tmp_path / "victim.db")
    link: Final = os.link

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
    mkdtemp: Final = tempfile.mkdtemp

    def create_then_swap(*, prefix: str, dir: Path) -> str:  # ruff:ignore[builtin-argument-shadowing]  # mirrors tempfile.mkdtemp
        directory: Final = Path(mkdtemp(prefix=prefix, dir=dir))
        directory.rename(tmp_path / "aside")
        directory.mkdir(mode=0o755)
        (directory / "notes.txt").write_text("keep")
        swapped_in.append(directory)
        return str(directory)

    mocker.patch("tempfile.mkdtemp", autospec=True, side_effect=create_then_swap)
    with pytest.raises(OSError, match="replaced while opening"):
        ReadWriteLock(database, is_singleton=False)
    assert [(directory / "notes.txt").read_text() for directory in swapped_in] == ["keep"]


def test_private_link_opens_nothing_through_a_swapped_link_directory(
    database: Path, tmp_path: Path, mocker: MockerFixture, created_directories: list[Path]
) -> None:
    # Whoever can rename in the lock directory may swap the link directory for one of its own before SQLite opens it.
    target: Final = tmp_path / "target.db"
    connect: Final = sqlite3.connect

    def swap_then_connect(
        database: str,
        *,
        check_same_thread: bool,
        factory: type[sqlite3.Connection],
        cached_statements: int,
        timeout: float,
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
        )

    mocker.patch("sqlite3.connect", autospec=True, side_effect=swap_then_connect)
    with pytest.raises(sqlite3.OperationalError, match="unable to open database file"):
        ReadWriteLock(database, is_singleton=False)
    assert (target.exists(), (created_directories[-1] / "lock.db").is_symlink()) == (False, True)


def test_private_link_release_keeps_a_directory_renamed_onto_the_link(database: Path, tmp_path: Path) -> None:
    other: Final = database.parent / "project"
    other.mkdir()
    (other / "notes.txt").write_text("keep")
    with ReadWriteLock(database, is_singleton=False).write_lock():
        (link_directory,) = _link_directories(database)
        link_directory.rename(tmp_path / "aside")
        other.rename(link_directory)

    assert (link_directory / "notes.txt").read_text() == "keep"


def test_private_link_sweeps_the_link_of_a_killed_holder(database: Path, holder: subprocess.Popen[str]) -> None:
    holder.kill()
    holder.wait(timeout=_PROCESS_DEADLINE)
    left: Final = _link_directories(database)
    with ReadWriteLock(database, is_singleton=False).read_lock():
        pass

    assert (len(left), _link_directories(database), database.stat().st_nlink) == (1, [], 1)


def test_private_link_sweep_removes_a_link_left_by_an_earlier_release(database: Path) -> None:
    # Releases up to 4.2.0 linked the database right inside the directory.
    database.touch()
    left: Final = database.parent / f".filelock-{2**31 - 1}--{_host_digest(database)}-x"
    left.mkdir(mode=0o700)
    (left / "lock.db").hardlink_to(database)
    with ReadWriteLock(database, is_singleton=False).read_lock():
        pass

    assert (_link_directories(database), database.stat().st_nlink) == ([], 1)


@pytest.mark.parametrize(
    "entry",
    [
        pytest.param("lock.db", id="flat"),
        pytest.param("inner/lock.db", id="nested"),
        pytest.param("inner/other", id="nested-without-link"),
    ],
)
def test_private_link_sweep_keeps_a_directory_named_like_a_dead_holder(database: Path, entry: str) -> None:
    named: Final = database.parent / f".filelock-{2**31 - 1}--{_host_digest(database)}-x"
    (named / entry).parent.mkdir(parents=True)
    (named / entry).write_text("keep")
    (named / "notes.txt").write_text("keep")
    ReadWriteLock(database, is_singleton=False).close()

    assert [(named / name).read_text() for name in (entry, "notes.txt")] == ["keep", "keep"]


def test_private_link_sweep_never_parses_a_directory_of_another_account(database: Path, mocker: MockerFixture) -> None:
    database.touch()
    planted: Final = database.parent / f".filelock-{2**31 - 1}--{_host_digest(database)}-x"
    planted.mkdir(mode=0o700)
    (planted / "inner").mkdir()
    (planted / "inner" / "lock.db").hardlink_to(database)
    real_fstat: Final = os.fstat

    def fstat_with_other_owner(fd: int) -> os.stat_result:
        status = real_fstat(fd)
        if status.st_ino != planted.stat().st_ino:
            return status
        return os.stat_result((
            status.st_mode,
            status.st_ino,
            status.st_dev,
            status.st_nlink,
            status.st_uid + 1,
            status.st_gid,
            status.st_size,
            status.st_atime,
            status.st_mtime,
            status.st_ctime,
        ))

    mocker.patch("os.fstat", autospec=True, side_effect=fstat_with_other_owner)
    mocker.patch("os.kill", autospec=True, side_effect=AssertionError("PID probed"))
    ReadWriteLock(database, is_singleton=False).close()

    assert ((planted / "inner" / "lock.db").exists(), database.stat().st_nlink) == (True, 2)


def test_private_link_sweep_skips_a_symlink_under_a_dead_holder_name(database: Path) -> None:
    # The symlink leads to the database's own directory.
    database.touch()
    named: Final = database.parent / f".filelock-{2**31 - 1}--{_host_digest(database)}-x"
    named.mkdir()
    (named / "inner").symlink_to(database.parent)
    ReadWriteLock(database, is_singleton=False).close()

    assert (database.exists(), (named / "inner").is_symlink()) == (True, True)


def test_private_link_sweep_skips_a_symlink_to_a_live_holder(database: Path, holder: subprocess.Popen[str]) -> None:
    (held,) = _link_directories(database)
    (inner,) = list(held.iterdir())
    named: Final = database.parent / f".filelock-{2**31 - 1}--{_host_digest(database)}-x"
    named.mkdir()
    (named / "inner").symlink_to(inner)
    ReadWriteLock(database, is_singleton=False).close()

    assert ((inner / "lock.db").exists(), holder.poll()) == (True, None)


@pytest.mark.usefixtures("holder")
def test_private_link_sweep_keeps_the_link_of_a_live_holder(database: Path) -> None:
    held: Final = _link_directories(database)
    with ReadWriteLock(database, is_singleton=False).read_lock():
        pass
    assert (len(held), _link_directories(database)) == (1, held)


def test_private_link_sweep_keeps_a_directory_it_did_not_name(database: Path) -> None:
    foreign: Final = database.parent / ".filelock-not-ours"
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
    malformed: Final = database.parent / f".filelock-{pid_text}-{start_text}-{_host_digest(database)}-x"
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


_PROCESS_DEADLINE: Final = 30


@pytest.fixture
def holder(database: Path) -> Generator[subprocess.Popen[str]]:
    script: Final = (
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


def _link_directories(database: Path) -> list[Path]:
    return sorted(path for path in database.parent.iterdir() if path.name.startswith(".filelock-"))


def _host_digest(database: Path) -> str:
    # Taken from a link of another database, so the sweep for this one has not run yet.
    seed: Final = database.parent.with_name("seed")
    seed.mkdir(mode=0o777)
    seed.chmod(0o777)
    with ReadWriteLock(seed / "lock.db", is_singleton=False).read_lock():
        return next(seed.glob(".filelock-*")).name.rsplit("-", 2)[1]


@pytest.fixture
def database(tmp_path: Path) -> Path:
    # Others may create names in this directory, so SQLite connects through a private link instead of the name.
    shared: Final = tmp_path / "shared"
    shared.mkdir()
    shared.chmod(0o777)
    return shared / "lock.db"


@pytest.fixture
def created_directories(mocker: MockerFixture) -> list[Path]:
    directories: Final[list[Path]] = []
    mkdtemp: Final = tempfile.mkdtemp

    def create_directory(*, prefix: str, dir: Path) -> str:  # ruff:ignore[builtin-argument-shadowing]  # mirrors tempfile.mkdtemp
        directory: Final = mkdtemp(prefix=prefix, dir=dir)
        directories.append(Path(directory))
        return directory

    mocker.patch("tempfile.mkdtemp", autospec=True, side_effect=create_directory)
    return directories
