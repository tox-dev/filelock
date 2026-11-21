from __future__ import annotations

import sys
import threading
import time
from typing import TYPE_CHECKING, Final, Literal

import pytest

from tests.capability_marks import NEEDS_GENERATOR_EXCEPTION_CONTEXT, NEEDS_PROMPT_FINALIZATION
from tests.read_write_helpers import writer_gets_in

pytest.importorskip("sqlite3")
import sqlite3

from filelock import ReadWriteLock, Timeout
from filelock._sqlite._connection import ForkSafeConnection

if TYPE_CHECKING:
    from types import BuiltinFunctionType, FrameType

    from pytest_mock import MockerFixture


pytestmark: Final[pytest.MarkDecorator] = pytest.mark.usefixtures("clear_sqlite_singletons")


@pytest.mark.parametrize(
    "mode",
    [
        pytest.param("read", id="read"),
        pytest.param("write", id="write"),
    ],
)
def test_busy_timeout_recomputed_after_journal_mode(
    lock_file: str, mocker: MockerFixture, mode: Literal["read", "write"]
) -> None:
    lock = ReadWriteLock(lock_file, is_singleton=False)
    clock = mocker.patch("filelock._sqlite._lock.time", autospec=True)
    mocker.patch("filelock._sqlite._transactions.time", clock)
    clock.perf_counter.return_value = 0.0
    clock.monotonic.side_effect = time.monotonic
    execute = ForkSafeConnection.execute
    statements: list[str] = []

    def execute_with_delay(connection: ForkSafeConnection, statement: str) -> sqlite3.Cursor:
        if statement.startswith("PRAGMA busy_timeout="):
            statements.append(statement)
            clock.perf_counter.return_value = 0.02
        return execute(connection, statement)

    mocker.patch.object(ForkSafeConnection, "execute", new=execute_with_delay)
    mocker.patch.object(threading, "active_count", autospec=True, return_value=1)
    getattr(lock, f"acquire_{mode}")(timeout=0.08)
    lock.release()
    assert statements == ["PRAGMA busy_timeout=80", "PRAGMA busy_timeout=60"]


def test_acquire_converts_connect_lock_error_to_timeout(lock_file: str, mocker: MockerFixture) -> None:
    lock = ReadWriteLock(lock_file, is_singleton=False)
    busy = sqlite3.OperationalError("database is locked")
    busy.sqlite_errorcode = sqlite3.SQLITE_BUSY
    mocker.patch("filelock._sqlite._transactions.connect", side_effect=busy, autospec=True)

    with pytest.raises(Timeout):
        lock.acquire_read(timeout=0.1)


@NEEDS_GENERATOR_EXCEPTION_CONTEXT
def test_finish_connection_chains_rollback_then_close_error(lock_file: str, mocker: MockerFixture) -> None:
    lock = ReadWriteLock(lock_file, is_singleton=False)
    con = mocker.create_autospec(sqlite3.Connection, instance=True)
    type(con).in_transaction = mocker.PropertyMock(  # simplint: ignore[mock-without-spec]  # C property has no autospec
        side_effect=[True, False]
    )
    con.rollback.side_effect = sqlite3.OperationalError("rollback boom")
    con.close.side_effect = sqlite3.OperationalError("close boom")
    lock._owned_connection.connection = con
    lock._owned_connection.transaction_released = False

    with pytest.raises(sqlite3.OperationalError, match="close boom") as excinfo:
        lock._finish_connection()

    assert isinstance(excinfo.value.__context__, sqlite3.OperationalError)
    assert "rollback boom" in str(excinfo.value.__context__)
    con.rollback.assert_called_once_with()
    con.close.assert_called_once_with()


def test_finish_connection_skips_rollback_when_already_released(lock_file: str) -> None:
    lock = ReadWriteLock(lock_file, is_singleton=False)
    lock.acquire_write()
    con = lock._con
    assert con is not None
    lock._owned_connection.transaction_released = True

    lock._finish_connection()

    assert lock._con is None
    with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
        con.execute("SELECT 1")


def test_read_lock_avoids_sqlite_schema_alias(lock_file: str, mocker: MockerFixture) -> None:
    # SQLite before 3.33.0 has no sqlite_schema alias, so a read lock issuing it fails with "no such table".
    real_connect = sqlite3.connect
    statements: list[str] = []

    def connect(
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
        connection.set_trace_callback(statements.append)
        return connection

    mocker.patch("sqlite3.connect", side_effect=connect, autospec=True)
    with ReadWriteLock(lock_file, is_singleton=False).read_lock():
        pass

    assert statements
    assert not [statement for statement in statements if "sqlite_schema" in statement]


@NEEDS_PROMPT_FINALIZATION
def test_dropping_a_lock_closes_its_connection(lock_file: str) -> None:
    # Nothing else closes a lock dropped while still holding one, so the connection would outlive it.
    lock = ReadWriteLock(lock_file, is_singleton=False)
    lock.acquire_read()

    del lock

    assert writer_gets_in(lock_file)


@NEEDS_PROMPT_FINALIZATION
@pytest.mark.parametrize("mode", ["read", "write"])
def test_singleton_dropped_while_held_is_not_handed_out_again(lock_file: str, mode: Literal["read", "write"]) -> None:
    lock = ReadWriteLock(lock_file)
    getattr(lock, f"acquire_{mode}")()
    looked_up: Final[list[ReadWriteLock]] = []

    # The cache lookup another thread makes while the dropped lock's connection closes. Interpreters run a profile
    # hook with tracing off, so coverage does not record its body.
    def look_up_while_closing(_frame: FrameType, event: str, arg: BuiltinFunctionType) -> None:  # pragma: no cover
        closing = event == "c_call" and getattr(arg, "__name__", None) == "close"
        if closing and isinstance(getattr(arg, "__self__", None), sqlite3.Connection) and not looked_up:
            looked_up.append(ReadWriteLock(lock_file))

    sys.setprofile(look_up_while_closing)
    try:
        del lock
    finally:
        sys.setprofile(None)

    with getattr(looked_up[0], f"{mode}_lock")(), pytest.raises(Timeout):
        ReadWriteLock(lock_file, is_singleton=False).acquire_write(blocking=False)


def test_connection_close_from_another_process_leaves_it_open(lock_file: str, mocker: MockerFixture) -> None:
    # A child inherits the parent's connection, and closing it there would take the parent's down with it.
    lock = ReadWriteLock(lock_file, is_singleton=False)
    lock.acquire_read()
    connection = lock._con
    assert connection is not None
    mocker.patch("filelock._sqlite._connection.GETPID", return_value=-1, autospec=True)

    connection.close()

    mocker.stopall()
    assert connection.execute("select 1").fetchone() == (1,)
    lock.release()
