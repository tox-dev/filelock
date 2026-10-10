from __future__ import annotations

import asyncio
import os
import shutil
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

    from pytest_mock import MockerFixture, MockType

pytestmark: Final = [
    pytest.mark.requires_hard_links,
    pytest.mark.skipif(
        not CAPABILITIES["posix-hard-link"],
        reason="private database aliases need POSIX hard links without following symlinks",
    ),
]


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS must avoid /dev/fd lookups")
@pytest.mark.usefixtures("macos_descriptor_guard")
@pytest.mark.asyncio
@pytest.mark.parametrize("mode", [pytest.param("read", id="read"), pytest.param("write", id="write")])
async def test_macos_connects_through_the_validated_inode(
    database: Path, mocker: MockerFixture, mode: Literal["read", "write"]
) -> None:  # pragma: darwin cover
    connect: Final = mocker.spy(sqlite3, "connect")
    lock: Final = AsyncReadWriteLock(database, is_singleton=False)
    try:
        async with (lock.read_lock if mode == "read" else lock.write_lock)():
            opened: Final = Path(connect.call_args.args[0])
            assert (opened.name, opened.parent.parent, await asyncio.to_thread(opened.samefile, database)) == (
                "lock.db",
                database.parent,
                True,
            )
            await asyncio.to_thread(assert_read_write_lock_state, str(database), "write", available=False)
    finally:
        await lock.close()


@pytest.mark.parametrize("mode", [pytest.param("read", id="read"), pytest.param("write", id="write")])
def test_missing_descriptor_path_preserves_contention(database: Path, mode: Literal["read", "write"]) -> None:
    lock: Final = ReadWriteLock(database, is_singleton=False)
    with (lock.read_lock if mode == "read" else lock.write_lock)():
        assert_read_write_lock_state(str(database), "write", available=False)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", [pytest.param("read", id="read"), pytest.param("write", id="write")])
async def test_missing_descriptor_path_preserves_async_contention(
    database: Path, mode: Literal["read", "write"]
) -> None:
    lock: Final = AsyncReadWriteLock(database, is_singleton=False)
    async with (lock.read_lock if mode == "read" else lock.write_lock)():
        assert_read_write_lock_state(str(database), "write", available=False)
    await lock.close()


def test_missing_descriptor_path_removes_alias_after_release(database: Path, created_directories: list[Path]) -> None:
    lock: Final = ReadWriteLock(database, is_singleton=False)
    lock.acquire_read()
    assert [directory.exists() for directory in created_directories] == [False, True]
    lock.release()
    assert [directory.exists() for directory in created_directories] == [False, False]


def test_missing_descriptor_path_links_beside_the_database(database: Path, created_directories: list[Path]) -> None:
    # A hard link cannot cross filesystems, so an alias in TMPDIR fails for a database stored anywhere else.
    ReadWriteLock(database, is_singleton=False).close()
    assert [directory.parent for directory in created_directories] == [database.parent]


def test_missing_descriptor_path_release_survives_a_removed_alias(
    database: Path, created_directories: list[Path]
) -> None:
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
def test_missing_descriptor_path_cleans_up_after_failure(
    database: Path, mocker: MockerFixture, boundary: str, created_directories: list[Path]
) -> None:
    mocker.patch(boundary, autospec=True, side_effect=OSError("cannot open database"))
    with pytest.raises(OSError, match="cannot open database"):
        ReadWriteLock(database, is_singleton=False)
    assert [directory.exists() for directory in created_directories] == [False]


@pytest.mark.parametrize("replacement_kind", [pytest.param("file", id="file"), pytest.param("symlink", id="symlink")])
def test_missing_descriptor_path_rejects_replaced_database(
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


def test_missing_descriptor_path_sweeps_the_link_of_a_killed_holder(
    database: Path, holder: subprocess.Popen[str]
) -> None:
    holder.kill()
    holder.wait(timeout=_PROCESS_DEADLINE)
    left: Final = _link_directories(database)
    with ReadWriteLock(database, is_singleton=False).read_lock():
        pass

    assert (len(left), _link_directories(database), database.stat().st_nlink) == (1, [], 1)


@pytest.mark.usefixtures("holder")
def test_missing_descriptor_path_keeps_the_link_of_a_live_holder(database: Path) -> None:
    held: Final = _link_directories(database)
    with ReadWriteLock(database, is_singleton=False).read_lock():
        pass
    assert (len(held), _link_directories(database)) == (1, held)


def test_missing_descriptor_path_keeps_a_directory_it_did_not_name(database: Path) -> None:
    foreign: Final = database.parent / ".filelock-not-ours"
    foreign.mkdir()
    with ReadWriteLock(database, is_singleton=False).read_lock():
        pass
    assert foreign.is_dir()


@NEEDS_FILE_PERMISSIONS  # pragma: needs file-permissions
def test_missing_descriptor_path_links_in_a_directory_it_cannot_list(database: Path) -> None:
    database.parent.chmod(0o300)
    try:
        with ReadWriteLock(database, is_singleton=False).read_lock():
            assert_read_write_lock_state(str(database), "write", available=False)
    finally:
        database.parent.chmod(0o700)


_PROCESS_DEADLINE: Final = 30


@pytest.fixture
def holder(database: Path) -> Generator[subprocess.Popen[str]]:
    script: Final = (
        "import os, sys, time\n"
        "os.access = lambda *args, **kwargs: False\n"
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


@pytest.fixture
def database(tmp_path: Path) -> Path:
    return tmp_path / "lock.db"


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


@pytest.fixture(autouse=True)
def missing_descriptor_path(mocker: MockerFixture) -> MockType:
    return mocker.patch("os.access", autospec=True, return_value=False)


@pytest.fixture
def macos_descriptor_guard(missing_descriptor_path: MockType) -> None:  # pragma: darwin cover
    missing_descriptor_path.side_effect = AssertionError("macOS must not look up descriptor paths")
