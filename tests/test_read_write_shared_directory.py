from __future__ import annotations

import os
import struct
from contextlib import closing
from pathlib import Path
from typing import TYPE_CHECKING, Final

import pytest
from capabilities import CAPABILITIES

pytest.importorskip("sqlite3")

import sqlite3

from filelock import ReadWriteLock
from tests.read_write_helpers import assert_read_write_lock_state

if TYPE_CHECKING:
    from pytest_mock import MockerFixture

pytestmark: Final = [
    pytest.mark.requires_hard_links,
    pytest.mark.skipif(
        not CAPABILITIES["posix-hard-link"],
        reason="a shared lock directory gets a private link to the database only where POSIX hard links work",
    ),
]

# https://www.sqlite.org/fileformat.html#the_rollback_journal
_JOURNAL_MAGIC: Final = bytes.fromhex("d9d505f920a163d7")


def test_shared_directory_ignores_a_journal_beside_the_database(database: Path, tmp_path: Path) -> None:
    # SQLite treats a journal beside a non-empty database as an interrupted transaction, and finishing it deletes the
    # super-journal file the journal names.
    named: Final = tmp_path / "notes.txt"
    named.write_text("keep")
    with closing(sqlite3.connect(database)) as connection:
        connection.execute("CREATE TABLE application (value)")
    encoded: Final = os.fsencode(named)
    Path(f"{database}-journal").write_bytes(encoded + struct.pack(">II", len(encoded), sum(encoded)) + _JOURNAL_MAGIC)

    with ReadWriteLock(database, is_singleton=False).read_lock():
        pass

    assert named.read_text() == "keep"


def test_shared_directory_ignores_a_directory_at_the_wal_name(database: Path) -> None:
    # SQLite removes a leftover -wal beside an empty database, so a name it cannot remove failed every acquire.
    wal: Final = Path(f"{database}-wal")
    wal.mkdir()
    (wal / "entry").touch()

    with ReadWriteLock(database, is_singleton=False).write_lock():
        assert_read_write_lock_state(str(database), "read", available=False)


def test_shared_directory_keeps_sqlite_off_the_name_of_an_unlinked_database(
    database: Path, tmp_path: Path, mocker: MockerFixture
) -> None:
    # The kernel names an open file whose name was unlinked "<name> (deleted)", a name anyone sharing the directory can
    # create; relinking the same inode under the database name keeps every check on the path satisfied.
    target: Final = tmp_path / "target.txt"
    Path(f"{database} (deleted)").symlink_to(target)
    database.touch()
    kept: Final = database.with_name("kept.db")
    kept.hardlink_to(database)
    connect: Final = sqlite3.connect

    def relink_then_connect(
        path: str,
        *,
        check_same_thread: bool,
        factory: type[sqlite3.Connection],
        cached_statements: int,
        timeout: float,
    ) -> sqlite3.Connection:
        database.unlink()
        database.hardlink_to(kept)
        return connect(
            path,
            check_same_thread=check_same_thread,
            factory=factory,
            cached_statements=cached_statements,
            timeout=timeout,
        )

    mocker.patch("sqlite3.connect", autospec=True, side_effect=relink_then_connect)

    ReadWriteLock(database, is_singleton=False).close()

    assert not target.exists()


def test_private_directory_connects_by_the_database_name(tmp_path: Path, mocker: MockerFixture) -> None:
    # Nobody else can create or rename names there, so SQLite may derive its side files beside the database.
    database: Final = tmp_path / "lock.db"
    connect: Final = mocker.spy(sqlite3, "connect")

    with ReadWriteLock(database, is_singleton=False).read_lock():
        assert_read_write_lock_state(str(database), "write", available=False)

    assert (connect.call_args.args[0], list(tmp_path.iterdir())) == (str(database), [database])


def test_private_directory_under_a_shared_parent_refuses_a_replaced_directory(
    database: Path, mocker: MockerFixture
) -> None:
    # Whoever can rename in the shared parent owns every name below it, so the private directory check cannot stop
    # SQLite from opening a replacement swapped in before it connects; the acquire only refuses to report the lock held.
    private: Final = database.parent / "private"
    private.mkdir(mode=0o700)
    lock: Final = ReadWriteLock(private / "lock.db", is_singleton=False)
    moved: Final = database.parent / "moved"
    connect: Final = sqlite3.connect

    def replace_then_connect(
        path: str,
        *,
        check_same_thread: bool,
        factory: type[sqlite3.Connection],
        cached_statements: int,
        timeout: float,
    ) -> sqlite3.Connection:
        private.rename(moved)
        private.mkdir(mode=0o700)
        (private / "lock.db").touch()
        return connect(
            path,
            check_same_thread=check_same_thread,
            factory=factory,
            cached_statements=cached_statements,
            timeout=timeout,
        )

    mocker.patch("sqlite3.connect", autospec=True, side_effect=replace_then_connect)

    with pytest.raises(OSError, match="replaced while acquiring"):
        lock.acquire_write()
    assert_read_write_lock_state(str(moved / "lock.db"), "write", available=True)


@pytest.fixture
def database(tmp_path: Path) -> Path:
    shared: Final = tmp_path / "shared"
    shared.mkdir()
    shared.chmod(0o777)
    return shared / "lock.db"
