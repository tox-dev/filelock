from __future__ import annotations

import asyncio
import errno
import os
import shutil
import stat
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Final, Literal
from urllib.parse import unquote, urlsplit

import pytest
from capabilities import CAPABILITIES

pytest.importorskip("sqlite3")
import sqlite3

from filelock import AsyncReadWriteLock, ReadWriteLock
from tests.capability_marks import NEEDS_FILE_PERMISSIONS
from tests.read_write_helpers import assert_read_write_lock_state
from tests.sqlite.link_support import link_directories

if TYPE_CHECKING:
    from collections.abc import Callable

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
    shared_database: Path, mocker: MockerFixture, mode: Literal["read", "write"]
) -> None:
    connect = mocker.spy(sqlite3, "connect")
    lock: Final[AsyncReadWriteLock] = AsyncReadWriteLock(shared_database, is_singleton=False)
    try:
        async with getattr(lock, f"{mode}_lock")():
            opened: Final[Path] = Path(unquote(urlsplit(connect.call_args.args[0]).path))
            link_directory: Final[Path] = opened.parents[2]
            assert (
                (connect.call_args.kwargs["uri"], urlsplit(connect.call_args.args[0]).query),
                opened.name,
                link_directory.parent,
                stat.S_IMODE(link_directory.stat().st_mode),
                await asyncio.to_thread(opened.samefile, shared_database),
            ) == ((True, "mode=rw"), "lock.db", shared_database.parent, 0o700, True)
            await asyncio.to_thread(assert_read_write_lock_state, str(shared_database), "write", available=False)
    finally:
        await lock.close()


def test_private_link_opens_when_its_directory_needs_uri_quoting(shared_database: Path) -> None:
    parent = shared_database.parent.with_name("shared? #%")
    shared_database.parent.rename(parent)
    shared_database = parent / shared_database.name
    with ReadWriteLock(shared_database, is_singleton=False).write_lock():
        assert_read_write_lock_state(str(shared_database), "write", available=False)


@pytest.mark.parametrize("mode", [pytest.param("read", id="read"), pytest.param("write", id="write")])
def test_private_link_preserves_contention(shared_database: Path, mode: Literal["read", "write"]) -> None:
    lock: Final[ReadWriteLock] = ReadWriteLock(shared_database, is_singleton=False)
    with getattr(lock, f"{mode}_lock")():
        assert_read_write_lock_state(str(shared_database), "write", available=False)


@pytest.mark.parametrize(
    "boundary", [pytest.param("tempfile.mkdtemp", id="mkdtemp"), pytest.param("os.link", id="link")]
)
def test_private_link_failure_preserves_a_peer_lock(
    shared_database: Path, mocker: MockerFixture, boundary: str
) -> None:
    with ReadWriteLock(shared_database, is_singleton=False).read_lock():
        mocker.patch(boundary, autospec=True, side_effect=PermissionError(errno.EACCES, "read-only directory"))
        with pytest.raises(PermissionError, match="read-only directory"):
            ReadWriteLock(shared_database, is_singleton=False)
        assert_read_write_lock_state(str(shared_database), "write", available=False)
    assert list(shared_database.parent.glob(".filelock-*")) == []


def test_private_link_removed_before_sqlite_open_cannot_grant_a_second_writer(
    shared_database: Path, mocker: MockerFixture, created_directories: list[Path]
) -> None:
    with ReadWriteLock(shared_database, is_singleton=False).write_lock():
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
            ReadWriteLock(shared_database, is_singleton=False)
        mocker.stop(patched)
        assert_read_write_lock_state(str(shared_database), "write", available=False)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", [pytest.param("read", id="read"), pytest.param("write", id="write")])
async def test_private_link_preserves_async_contention(shared_database: Path, mode: Literal["read", "write"]) -> None:
    lock: Final[AsyncReadWriteLock] = AsyncReadWriteLock(shared_database, is_singleton=False)
    async with getattr(lock, f"{mode}_lock")():
        assert_read_write_lock_state(str(shared_database), "write", available=False)
    await lock.close()


def test_private_link_is_removed_after_release(shared_database: Path, created_directories: list[Path]) -> None:
    lock: Final[ReadWriteLock] = ReadWriteLock(shared_database, is_singleton=False)
    lock.acquire_read()
    assert [directory.exists() for directory in created_directories] == [False, True]
    lock.release()
    assert [directory.exists() for directory in created_directories] == [False, False]


def test_private_link_sits_beside_the_database(shared_database: Path, created_directories: list[Path]) -> None:
    # A hard link cannot cross filesystems, so an alias in TMPDIR fails for a database stored anywhere else.
    ReadWriteLock(shared_database, is_singleton=False).close()
    assert [directory.parent for directory in created_directories] == [shared_database.parent]


def test_private_link_release_survives_a_removed_alias(shared_database: Path, created_directories: list[Path]) -> None:
    lock: Final[ReadWriteLock] = ReadWriteLock(shared_database, is_singleton=False)
    lock.acquire_read()
    shutil.rmtree(created_directories[-1])
    lock.release()

    with ReadWriteLock(shared_database, is_singleton=False).write_lock(blocking=False):
        assert_read_write_lock_state(str(shared_database), "write", available=False)


@pytest.mark.parametrize(
    "boundary",
    [pytest.param("os.link", id="link"), pytest.param("sqlite3.connect", id="connect")],
)
def test_private_link_is_removed_after_failure(
    shared_database: Path, mocker: MockerFixture, boundary: str, created_directories: list[Path]
) -> None:
    mocker.patch(boundary, autospec=True, side_effect=OSError("cannot open shared_database"))
    with pytest.raises(OSError, match="cannot open shared_database"):
        ReadWriteLock(shared_database, is_singleton=False)
    assert [directory.exists() for directory in created_directories] == [False]


@pytest.mark.parametrize(
    "make_replacement",
    [
        pytest.param(Path.touch, id="file"),
        pytest.param(lambda path: path.symlink_to(path.parent / "victim.db"), id="symlink"),
    ],
)
def test_private_link_rejects_replaced_database(
    shared_database: Path,
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
        ReadWriteLock(shared_database, is_singleton=False)
    assert ([directory.exists() for directory in created_directories], (tmp_path / "victim.db").exists()) == (
        [False],
        False,
    )


def test_private_link_refuses_a_directory_swapped_in_before_it_opens(
    shared_database: Path, tmp_path: Path, mocker: MockerFixture
) -> None:
    swapped_in: Final[list[Path]] = []
    mkdtemp = tempfile.mkdtemp

    def create_then_swap(
        *,
        prefix: str,
        dir: Path,  # ruff:ignore[builtin-argument-shadowing]  # matches mkdtemp
    ) -> str:
        directory: Final[Path] = Path(mkdtemp(prefix=prefix, dir=dir))
        directory.rename(tmp_path / "aside")
        directory.mkdir(mode=0o755)
        (directory / "notes.txt").write_text("keep")
        swapped_in.append(directory)
        return str(directory)

    mocker.patch("tempfile.mkdtemp", autospec=True, side_effect=create_then_swap)
    with pytest.raises(OSError, match="replaced while opening"):
        ReadWriteLock(shared_database, is_singleton=False)
    assert [(directory / "notes.txt").read_text() for directory in swapped_in] == ["keep"]


def test_private_link_repairs_inherited_permissions_on_the_nested_directory(
    shared_database: Path, mocker: MockerFixture
) -> None:
    mkdir = os.mkdir

    def inherit_permissions(path: str, mode: int = 0o777, *, dir_fd: int | None = None) -> None:
        mkdir(path, mode, dir_fd=dir_fd)
        if dir_fd is not None:
            os.chmod(path, stat.S_IRWXU | stat.S_IRGRP, dir_fd=dir_fd)

    mocker.patch("os.mkdir", autospec=True, side_effect=inherit_permissions)
    with ReadWriteLock(shared_database, is_singleton=False).read_lock():
        directory = next(shared_database.parent.glob(".filelock-*/*"))
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700

    mocker.patch("os.fchmod", autospec=True)
    with pytest.raises(PermissionError, match="inherited ACL"):
        ReadWriteLock(shared_database, is_singleton=False)
    assert list(shared_database.parent.glob(".filelock-*")) == []


def test_private_link_closes_a_mode_an_inherited_acl_opened(
    shared_database: Path, loosened_directories: list[Path]
) -> None:
    lock: Final[ReadWriteLock] = ReadWriteLock(shared_database, is_singleton=False)
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
    shared_database: Path, loosened_directories: list[Path], mocker: MockerFixture, fchmod_error: PermissionError | None
) -> None:
    # ZFS with aclmode=restricted refuses a chmod on a directory carrying an inherited ACL.
    mocker.patch("os.fchmod", autospec=True, side_effect=fchmod_error)
    with pytest.raises(OSError, match="writable by its owner only"):
        ReadWriteLock(shared_database, is_singleton=False)
    assert len(loosened_directories) == 1


def test_private_link_opens_nothing_through_a_swapped_link_directory(
    shared_database: Path, tmp_path: Path, mocker: MockerFixture, created_directories: list[Path]
) -> None:
    # Whoever can rename in the lock directory may swap the link directory for one of its own before SQLite opens it.
    target: Final[Path] = tmp_path / "target.db"
    connect = sqlite3.connect

    def swap_then_connect(
        shared_database: str,
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
            shared_database,
            check_same_thread=check_same_thread,
            factory=factory,
            cached_statements=cached_statements,
            timeout=timeout,
            uri=uri,
        )

    mocker.patch("sqlite3.connect", autospec=True, side_effect=swap_then_connect)
    with pytest.raises(sqlite3.OperationalError, match="unable to open database file"):
        ReadWriteLock(shared_database, is_singleton=False)
    assert (target.exists(), (created_directories[-1] / "lock.db").is_symlink()) == (False, True)


def test_private_link_refuses_a_watched_swapped_directory(
    shared_database: Path, tmp_path: Path, mocker: MockerFixture
) -> None:
    target = tmp_path / "target.db"
    watched = shared_database.parent / "results"
    watched.mkdir(mode=0o755)
    adopted: list[Path] = []
    mkdtemp = tempfile.mkdtemp
    connect = sqlite3.connect

    def create_then_swap(
        *,
        prefix: str,
        dir: Path,  # ruff:ignore[builtin-argument-shadowing]  # matches mkdtemp
    ) -> str:
        directory = Path(mkdtemp(prefix=prefix, dir=dir))
        watched.rename(directory)
        adopted.append(directory)
        return str(directory)

    def swap_then_connect(
        shared_database: str,
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
            shared_database,
            check_same_thread=check_same_thread,
            factory=factory,
            cached_statements=cached_statements,
            timeout=timeout,
            uri=uri,
        )

    mocker.patch("tempfile.mkdtemp", autospec=True, side_effect=create_then_swap)
    mocker.patch("sqlite3.connect", autospec=True, side_effect=swap_then_connect)
    with pytest.raises(sqlite3.OperationalError, match="unable to open database file"):
        ReadWriteLock(shared_database, is_singleton=False)
    assert target.exists() is False


def test_private_link_release_keeps_a_directory_renamed_onto_the_link(shared_database: Path, tmp_path: Path) -> None:
    other: Final[Path] = shared_database.parent / "project"
    other.mkdir()
    (other / "notes.txt").write_text("keep")
    with ReadWriteLock(shared_database, is_singleton=False).write_lock():
        (link_directory,) = link_directories(shared_database)
        link_directory.rename(tmp_path / "aside")
        other.rename(link_directory)

    assert (link_directory / "notes.txt").read_text() == "keep"


@NEEDS_FILE_PERMISSIONS  # pragma: needs file-permissions
def test_private_link_in_a_directory_it_cannot_list(shared_database: Path) -> None:
    shared_database.parent.chmod(0o333)
    try:
        with ReadWriteLock(shared_database, is_singleton=False).read_lock():
            assert_read_write_lock_state(str(shared_database), "write", available=False)
    finally:
        shared_database.parent.chmod(0o777)


_PROCESS_DEADLINE: Final[int] = 30


@pytest.fixture
def created_directories(mocker: MockerFixture) -> list[Path]:
    directories: Final[list[Path]] = []
    mkdtemp = tempfile.mkdtemp

    def create_directory(
        *,
        prefix: str,
        dir: Path,  # ruff:ignore[builtin-argument-shadowing]  # matches mkdtemp
    ) -> str:
        directory: Final[str] = mkdtemp(prefix=prefix, dir=dir)
        directories.append(Path(directory))
        return directory

    mocker.patch("tempfile.mkdtemp", autospec=True, side_effect=create_directory)
    return directories


@pytest.fixture
def loosened_directories(mocker: MockerFixture) -> list[Path]:
    directories: Final[list[Path]] = []
    mkdtemp = tempfile.mkdtemp

    def create_with_group_access(
        *,
        prefix: str,
        dir: Path,  # ruff:ignore[builtin-argument-shadowing]  # matches mkdtemp
    ) -> str:
        # Stands in for an inheritable ACL entry that grants the group access to every new directory.
        directory: Final[Path] = Path(mkdtemp(prefix=prefix, dir=dir))
        directory.chmod(0o770)
        directories.append(directory)
        return str(directory)

    mocker.patch("tempfile.mkdtemp", autospec=True, side_effect=create_with_group_access)
    return directories
