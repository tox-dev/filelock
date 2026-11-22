from __future__ import annotations

import os
import sys
from contextlib import closing
from pathlib import Path
from typing import TYPE_CHECKING, Final

import pytest

pytest.importorskip("sqlite3")
import sqlite3

from filelock import ReadWriteLock
from tests.capability_marks import SKIP_ON_UNRELIABLE_PROCESS_SYNC
from tests.read_write_helpers import assert_read_write_lock_state

if TYPE_CHECKING:
    from pytest_mock import MockerFixture

import struct

from capabilities import CAPABILITIES

_NEEDS_POSIX_HARD_LINK: Final[pytest.MarkDecorator] = pytest.mark.skipif(
    not CAPABILITIES["posix-hard-link"],
    reason="a shared lock directory gets a private link to the database only where POSIX hard links work",
)

pytestmark: Final[pytest.MarkDecorator] = SKIP_ON_UNRELIABLE_PROCESS_SYNC


@pytest.mark.requires_hard_links
@_NEEDS_POSIX_HARD_LINK
def test_shared_directory_ignores_a_journal_beside_the_database(  # pragma: needs posix-hard-link
    shared_database: Path, tmp_path: Path
) -> None:
    named = tmp_path / "notes.txt"
    named.write_text("keep")
    with closing(sqlite3.connect(shared_database)) as connection:
        connection.execute("CREATE TABLE application (value)")
    Path(f"{shared_database}-journal").write_bytes(_hot_journal(named))

    with ReadWriteLock(shared_database, is_singleton=False).read_lock():
        pass

    assert named.read_text() == "keep"


def _hot_journal(super_journal: Path) -> bytes:  # pragma: needs posix-hard-link
    # A journal whose tail names a super-journal makes SQLite unlink that file once it finishes the journal:
    # https://www.sqlite.org/fileformat.html#the_rollback_journal
    name = os.fsencode(super_journal)
    return name + struct.pack(">II", len(name), sum(name)) + bytes.fromhex("d9d505f920a163d7")


@pytest.mark.requires_hard_links
@_NEEDS_POSIX_HARD_LINK
def test_closed_directory_ignores_a_journal_left_beside_the_database(  # pragma: needs posix-hard-link
    shared_database: Path, tmp_path: Path
) -> None:
    named = tmp_path / "notes.txt"
    named.write_text("keep")
    with closing(sqlite3.connect(shared_database)) as connection:
        connection.execute("CREATE TABLE application (value)")
    Path(f"{shared_database}-journal").write_bytes(_hot_journal(named))
    shared_database.parent.chmod(0o755)

    with ReadWriteLock(shared_database, is_singleton=False).read_lock():
        pass

    assert named.read_text() == "keep"


@pytest.mark.requires_hard_links
@_NEEDS_POSIX_HARD_LINK
def test_shared_directory_ignores_a_directory_at_the_wal_name(  # pragma: needs posix-hard-link
    shared_database: Path,
) -> None:
    wal = Path(f"{shared_database}-wal")
    wal.mkdir()
    (wal / "entry").touch()

    with ReadWriteLock(shared_database, is_singleton=False).write_lock():
        assert_read_write_lock_state(str(shared_database), "read", available=False)


@pytest.mark.skipif(sys.platform != "win32", reason="a Windows directory is shared through its ACL")
def test_windows_shared_directory_ignores_a_journal_beside_the_database(  # pragma: win32 cover
    everyone_writable_database: Path, tmp_path: Path
) -> None:
    named = tmp_path / "notes.txt"
    named.write_text("keep")
    with closing(sqlite3.connect(everyone_writable_database)) as connection:
        connection.execute("CREATE TABLE application (value)")
    Path(f"{everyone_writable_database}-journal").write_bytes(_hot_journal(named))

    with ReadWriteLock(everyone_writable_database, is_singleton=False).read_lock():
        pass

    assert named.read_text() == "keep"


@pytest.mark.skipif(sys.platform != "win32", reason="a Windows directory is shared through its ACL")
def test_windows_shared_directory_ignores_an_undeletable_file_at_the_wal_name(  # pragma: win32 cover
    everyone_writable_database: Path,
) -> None:
    with Path(f"{everyone_writable_database}-wal").open("wb") as wal:
        wal.write(b"\0" * 32)
        wal.flush()

        with ReadWriteLock(everyone_writable_database, is_singleton=False).write_lock():
            assert_read_write_lock_state(str(everyone_writable_database), "read", available=False)


@pytest.mark.skipif(sys.platform != "win32", reason="a Windows directory is shared through its ACL")
def test_windows_private_directory_ignores_a_journal_left_beside_the_database(  # pragma: win32 cover
    tmp_path: Path,
) -> None:
    named = tmp_path / "notes.txt"
    named.write_text("keep")
    database = tmp_path / "lock.db"
    with closing(sqlite3.connect(database)) as connection:
        connection.execute("CREATE TABLE application (value)")
    Path(f"{database}-journal").write_bytes(_hot_journal(named))

    with ReadWriteLock(database, is_singleton=False).read_lock():
        pass

    assert named.read_text() == "keep"


@pytest.mark.requires_hard_links
@_NEEDS_POSIX_HARD_LINK
def test_shared_directory_keeps_sqlite_off_the_name_of_an_unlinked_database(  # pragma: needs posix-hard-link
    shared_database: Path, tmp_path: Path, mocker: MockerFixture
) -> None:
    # The kernel names an open file whose name was unlinked "<name> (deleted)", a name anyone sharing the directory can
    # create; relinking the same inode under the database name keeps every check on the path satisfied.
    target = tmp_path / "target.txt"
    Path(f"{shared_database} (deleted)").symlink_to(target)
    shared_database.touch()
    kept = shared_database.with_name("kept.db")
    kept.hardlink_to(shared_database)
    connect = sqlite3.connect

    def relink_then_connect(
        path: str,
        *,
        check_same_thread: bool,
        factory: type[sqlite3.Connection],
        cached_statements: int,
        timeout: float,
        uri: bool = False,
    ) -> sqlite3.Connection:
        shared_database.unlink()
        shared_database.hardlink_to(kept)
        return connect(
            path,
            check_same_thread=check_same_thread,
            factory=factory,
            cached_statements=cached_statements,
            timeout=timeout,
            uri=uri,
        )

    mocker.patch("sqlite3.connect", autospec=True, side_effect=relink_then_connect)

    ReadWriteLock(shared_database, is_singleton=False).close()

    assert not target.exists()


@pytest.mark.requires_hard_links
@_NEEDS_POSIX_HARD_LINK
def test_private_directory_connects_by_the_database_name(  # pragma: needs posix-hard-link
    tmp_path: Path, mocker: MockerFixture
) -> None:
    # Nobody else can create or rename names there, so SQLite may derive its side files beside the database.
    database = tmp_path / "lock.db"
    connect = mocker.spy(sqlite3, "connect")

    with ReadWriteLock(database, is_singleton=False).read_lock():
        assert_read_write_lock_state(str(database), "write", available=False)

    assert (connect.call_args.args[0], list(tmp_path.iterdir())) == (str(database), [database])


@pytest.mark.requires_hard_links
@_NEEDS_POSIX_HARD_LINK
@pytest.mark.parametrize(
    "replacement", [pytest.param("database", id="database"), pytest.param("symlink", id="symlink")]
)
def test_private_directory_under_a_shared_parent_refuses_a_replaced_directory(  # pragma: needs posix-hard-link
    shared_database: Path, mocker: MockerFixture, tmp_path: Path, replacement: str
) -> None:
    # A rename in the shared parent owns every name below it, so the private directory check cannot stop SQLite from
    # opening a replacement swapped in before it connects; the acquire refuses before any statement runs, so a hot
    # journal beside the replacement is neither replayed nor finished.
    private = shared_database.parent / "private"
    private.mkdir(mode=0o700)
    lock = ReadWriteLock(private / "lock.db", is_singleton=False)
    moved = shared_database.parent / "moved"
    (private / "notes.txt").write_text("keep")
    connect = sqlite3.connect
    before: dict[Path, list[tuple[str, bytes | None]]] = {moved: _entries(private)}

    def replace_then_connect(
        path: str,
        *,
        check_same_thread: bool,
        factory: type[sqlite3.Connection],
        cached_statements: int,
        timeout: float,
        uri: bool = False,
    ) -> sqlite3.Connection:
        private.rename(moved)
        private.mkdir(mode=0o700)
        if replacement == "symlink":
            (private / "lock.db").symlink_to(tmp_path / "elsewhere.txt")
        else:
            with closing(connect(private / "lock.db")) as created:
                created.execute("CREATE TABLE application (value)")
            (private / "lock.db-journal").write_bytes(_hot_journal(moved / "notes.txt"))
            (private / "lock.db-wal").write_bytes(b"\0" * 32)
            before[private] = _entries(private)
        return connect(
            path,
            check_same_thread=check_same_thread,
            factory=factory,
            cached_statements=cached_statements,
            timeout=timeout,
            uri=uri,
        )

    mocker.patch("sqlite3.connect", autospec=True, side_effect=replace_then_connect)

    with pytest.raises(OSError, match="replaced while acquiring"):
        lock.acquire_write()
    if replacement == "symlink":  # simplint: ignore[conditional-in-test]  # distinct outcomes
        # SQLite's open creates the missing name, which a symlink there sends to its target.
        assert ((tmp_path / "elsewhere.txt").read_bytes(), (moved / "notes.txt").read_text()) == (b"", "keep")
    else:
        assert {directory: _entries(directory) for directory in before} == before
    assert_read_write_lock_state(str(moved / "lock.db"), "write", available=True)


def _entries(directory: Path) -> list[tuple[str, bytes | None]]:  # pragma: needs posix-hard-link
    return sorted((path.name, path.read_bytes() if path.is_file() else None) for path in directory.iterdir())
