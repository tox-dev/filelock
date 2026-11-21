from __future__ import annotations

import os
import re
import subprocess  # ruff:ignore[suspicious-subprocess-import]  # only fixed-argument, no-shell runs below
import sys
from contextlib import closing
from pathlib import Path
from typing import TYPE_CHECKING, Final, Literal

import pytest
from capabilities import CAPABILITIES

pytest.importorskip("sqlite3")
import sqlite3

from filelock import ReadWriteLock
from filelock._sqlite._connection import ForkSafeConnection
from tests.capability_marks import (
    NEEDS_FIFO,
    NEEDS_FOREIGN_STICKY_TMP,
    NEEDS_SYMLINK,
    SKIP_ON_UNRELIABLE_PROCESS_SYNC,
)
from tests.read_write_helpers import assert_read_write_lock_state
from tests.sqlite.process_support import grant_everyone_write

if TYPE_CHECKING:
    from collections.abc import Callable

    from pytest_mock import MockerFixture


pytestmark: Final[pytest.MarkDecorator] = SKIP_ON_UNRELIABLE_PROCESS_SYNC


class _SubclassedReadWriteLock(ReadWriteLock):
    pass


@pytest.mark.usefixtures("connect_route")
@pytest.mark.parametrize("held_mode", [pytest.param("read", id="read"), pytest.param("write", id="write")])
@pytest.mark.parametrize(
    "open_other",
    [
        pytest.param(lambda path: ReadWriteLock(path, is_singleton=False), id="non-singleton"),
        pytest.param(_SubclassedReadWriteLock, id="subclass-singleton"),
        pytest.param(
            lambda path: ReadWriteLock(_hard_link(path), is_singleton=False),
            id="hard-link-alias",
            marks=pytest.mark.requires_hard_links,
        ),
    ],
)
def test_another_instance_on_the_database_keeps_the_held_lock(
    tmp_path: Path, held_mode: Literal["read", "write"], open_other: Callable[[Path], ReadWriteLock]
) -> None:
    # Only another process can observe whether closing the descriptor dropped this process's lock.
    lock_file = tmp_path / "lock.db"
    holder = ReadWriteLock(lock_file, is_singleton=False)
    getattr(holder, f"acquire_{held_mode}")()
    open_other(lock_file).close()
    assert_read_write_lock_state(str(lock_file), "write", available=False)
    holder.close()


def _hard_link(path: Path) -> Path:  # pragma: needs hard-link
    alias = path.with_name("alias.db")
    os.link(path, alias)
    return alias


_DESCRIPTOR_DIRECTORY: Final[Path] = Path("/proc/self/fd" if sys.platform == "linux" else "/dev/fd")


@pytest.mark.usefixtures("connect_route")
@pytest.mark.skipif(sys.platform == "win32", reason="the descriptor table is not listable on Windows")
def test_closing_every_instance_closes_its_descriptors(lock_file: str) -> None:
    descriptor_directory = _DESCRIPTOR_DIRECTORY  # pragma: win32 no cover
    before = len(list(descriptor_directory.iterdir()))  # pragma: win32 no cover
    holder = ReadWriteLock(lock_file, is_singleton=False)  # pragma: win32 no cover
    holder.acquire_read()  # pragma: win32 no cover
    ReadWriteLock(lock_file, is_singleton=False).close()  # pragma: win32 no cover
    holder.close()  # pragma: win32 no cover
    assert len(list(descriptor_directory.iterdir())) == before  # pragma: win32 no cover


@pytest.fixture(
    params=[
        pytest.param(False, id="private-directory"),
        pytest.param(
            True,
            id="shared-directory",
            marks=[
                pytest.mark.requires_hard_links,
                pytest.mark.skipif(
                    not CAPABILITIES["sqlite-private-link"],
                    reason="SQLite cannot create a private link on this filesystem",
                ),
            ],
        ),
    ]
)
def connect_route(request: pytest.FixtureRequest, tmp_path: Path) -> None:
    # Where others may create names beside the database, SQLite connects through a private link instead of the name.
    if request.param and sys.platform == "win32":  # pragma: win32 cover
        grant_everyone_write(tmp_path)
    elif request.param:  # pragma: needs posix-hard-link
        tmp_path.chmod(0o777)


def test_locks_alive_at_shutdown_finalize_silently(tmp_path: Path) -> None:
    # sys.excepthook outlives the stdlib module dicts teardown empties, so the finalizers run after os.getpid and
    # sqlite3.Error read as None.
    script = f"""
import sys
from filelock import AsyncReadWriteLock, ReadWriteLock
held = ReadWriteLock({str(tmp_path / "a.db")!r}, is_singleton=False)
held.acquire_read()
locks = held, AsyncReadWriteLock({str(tmp_path / "b.db")!r}, is_singleton=False)
sys.excepthook = lambda *args, _keep=locks, _hook=sys.excepthook: _hook(*args)
"""
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, check=True)

    assert not result.stderr


@NEEDS_SYMLINK
def test_read_write_lock_refuses_a_symlinked_path(tmp_path: Path) -> None:  # pragma: needs symlink
    # Following it would let anyone who can create names in the lock directory make the victim create the target.
    (link := tmp_path / "lock.db").symlink_to(target := tmp_path / "target.db")
    # Construction opens a connection, so the refusal lands there; acquiring reopens and refuses the same way.
    with pytest.raises(OSError, match=re.escape(str(link))):
        ReadWriteLock(link, is_singleton=False).acquire_read()
    assert not target.exists()


@NEEDS_FIFO
def test_read_write_lock_refuses_a_non_regular_path(tmp_path: Path) -> None:
    # A FIFO planted at the lock path opens without blocking (O_NONBLOCK) but is not a database to hand SQLite.
    lock_path = tmp_path / "lock.db"  # pragma: win32 no cover
    assert sys.platform != "win32"  # narrows os.mkfifo for the type checker  # pragma: win32 no cover
    os.mkfifo(lock_path)  # pragma: win32 no cover
    with pytest.raises(OSError, match="non-regular"):  # pragma: win32 no cover
        ReadWriteLock(lock_path, is_singleton=False)


@NEEDS_FOREIGN_STICKY_TMP
@pytest.mark.usefixtures("directories_of_another_account")
def test_read_write_lock_refuses_a_database_of_another_account(  # pragma: needs foreign-sticky-tmp
    lock_path_in_shared_tmp: Path,
) -> None:
    # Its owner could unlink it during a hold, letting a second process open a new database at the same path.
    lock_path_in_shared_tmp.touch()
    with pytest.raises(PermissionError, match="not to this process's account") as raised:
        ReadWriteLock(lock_path_in_shared_tmp, is_singleton=False)
    # The error names the path the lock resolved, and /tmp is a symlink to /private/tmp on macOS.
    assert (raised.value.filename, lock_path_in_shared_tmp.exists()) == (str(lock_path_in_shared_tmp.resolve()), True)


@pytest.mark.requires_hard_links  # pragma: needs hard-link
@NEEDS_FOREIGN_STICKY_TMP
def test_read_write_lock_checks_owner_of_reused_probe(  # pragma: needs foreign-sticky-tmp
    tmp_path: Path, lock_path_in_shared_tmp: Path, mocker: MockerFixture
) -> None:
    source = tmp_path / "source.db"
    source.touch()
    os.link(source, lock_path_in_shared_tmp)
    first = ReadWriteLock(source, is_singleton=False)
    second = ReadWriteLock(source, is_singleton=False)
    with first.read_lock():
        with second.read_lock():
            pass
        mocker.patch("filelock._sqlite._connection.foreign_owner", autospec=True, return_value=source.stat().st_uid + 1)
        with pytest.raises(PermissionError, match="not to this process's account"):
            ReadWriteLock(lock_path_in_shared_tmp, is_singleton=False)


def test_read_write_lock_refuses_a_wal_database(lock_file: str) -> None:
    _convert_to_wal(lock_file)
    with pytest.raises(ValueError, match="WAL-mode"):
        ReadWriteLock(lock_file, is_singleton=False)


def _convert_to_wal(path: str) -> None:
    with closing(sqlite3.connect(path)) as connection:
        connection.executescript("PRAGMA journal_mode=WAL; CREATE TABLE application (value);").close()


@pytest.mark.parametrize(
    "versions",
    [pytest.param(b"\x03\x01", id="read-version-1"), pytest.param(b"\x03\x00", id="read-version-0")],
)
def test_read_write_lock_refuses_read_only_sqlite_header(tmp_path: Path, versions: bytes) -> None:
    database = tmp_path / "lock.db"
    with closing(sqlite3.connect(database)) as connection:
        connection.execute("PRAGMA user_version=1")
    page = bytearray(database.read_bytes())
    page[18:20] = versions
    database.write_bytes(page)

    with pytest.raises(ValueError, match="unsupported SQLite format versions"):
        ReadWriteLock(database, is_singleton=False)
    assert database.read_bytes() == page


def test_writer_refuses_a_read_only_connection(lock_file: str, mocker: MockerFixture) -> None:
    lock = ReadWriteLock(lock_file, is_singleton=False)
    with closing(sqlite3.connect(f"{Path(lock_file).as_uri()}?mode=ro", uri=True, factory=ForkSafeConnection)) as conn:
        patched = mocker.patch.object(sqlite3, "connect", return_value=conn, autospec=True)
        with pytest.raises(ValueError, match="SQLite opened the lock database read-only"):
            lock.acquire_write(timeout=0)
        mocker.stop(patched)
    lock.acquire_write(timeout=0)
    lock.release()
    assert Path(lock_file).read_bytes() == b""


def test_read_write_lock_refuses_a_database_converted_to_wal_after_construction(lock_file: str) -> None:
    lock = ReadWriteLock(lock_file, is_singleton=False)
    _convert_to_wal(lock_file)
    with pytest.raises(ValueError, match="WAL-mode"):
        lock.acquire_read()
    lock.close()


@pytest.mark.parametrize("mode", [pytest.param("read", id="read"), pytest.param("write", id="write")])
def test_read_write_lock_refuses_wal_switch_during_acquire(
    lock_file: str, mocker: MockerFixture, mode: Literal["read", "write"]
) -> None:
    lock = ReadWriteLock(lock_file, is_singleton=False)
    execute = ForkSafeConnection.execute

    def switch_before_begin(connection: ForkSafeConnection, statement: str) -> sqlite3.Cursor:
        if statement.startswith("BEGIN"):
            with closing(sqlite3.connect(lock_file)) as peer:
                peer.executescript("PRAGMA journal_mode=WAL; PRAGMA user_version=1;")
        return execute(connection, statement)

    mocker.patch.object(ForkSafeConnection, "execute", new=switch_before_begin)
    with pytest.raises(ValueError, match="switched to WAL mode while acquiring"):
        getattr(lock, f"acquire_{mode}")(blocking=False)
    with pytest.raises(RuntimeError, match="not held"):
        lock.release()
    lock.close()


@pytest.mark.parametrize("replace", [pytest.param(False, id="unlinked"), pytest.param(True, id="replaced")])
@pytest.mark.skipif(
    sys.platform == "win32", reason="SQLite holds the database open without delete sharing, so its name cannot move"
)
def test_read_write_lock_refuses_a_database_moved_after_connect(
    tmp_path: Path, mocker: MockerFixture, *, replace: bool
) -> None:
    # A waiter connects, then the path moves on before its BEGIN lands; the lock it takes then excludes no later opener.
    lock_path = tmp_path / "lock.db"  # pragma: win32 no cover
    lock = ReadWriteLock(lock_path, is_singleton=False)  # pragma: win32 no cover
    real_connect = sqlite3.connect  # pragma: win32 no cover

    def connect_then_move(  # pragma: win32 no cover
        database: str,
        *,
        check_same_thread: bool,
        factory: type[sqlite3.Connection],
        cached_statements: int,
        timeout: float,
        uri: bool = False,
    ) -> sqlite3.Connection:
        connection = real_connect(
            database,
            check_same_thread=check_same_thread,
            factory=factory,
            cached_statements=cached_statements,
            timeout=timeout,
            uri=uri,
        )
        if replace:
            (replacement := tmp_path / "replacement.db").touch()
            replacement.replace(lock_path)
        else:
            lock_path.unlink()
        return connection

    mocker.patch("sqlite3.connect", side_effect=connect_then_move, autospec=True)  # pragma: win32 no cover
    with pytest.raises(OSError, match="unlinked or replaced"):  # pragma: win32 no cover
        lock.acquire_write()
    lock.close()  # pragma: win32 no cover
