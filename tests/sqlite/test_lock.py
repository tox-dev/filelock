from __future__ import annotations

import sys
import threading
import time
from errno import EWOULDBLOCK
from importlib import import_module
from multiprocessing import Event, Process, Value
from textwrap import dedent
from typing import TYPE_CHECKING, Final, Literal

import pytest
from capabilities import CAPABILITIES

pytest.importorskip("sqlite3")
from filelock import ReadWriteLock, Timeout
from tests.capability_marks import SKIP_ON_UNRELIABLE_PROCESS_SYNC
from tests.process_helpers import cleanup_processes
from tests.read_write_helpers import assert_read_write_lock_state, writer_gets_in
from tests.sqlite.process_support import PROCESS_DEADLINE, acquire_lock

if TYPE_CHECKING:
    import sqlite3
    from multiprocessing.sharedctypes import Synchronized
    from multiprocessing.synchronize import Event as EventType
    from pathlib import Path

    from pytest_mock import MockerFixture


pytestmark: Final[pytest.MarkDecorator] = SKIP_ON_UNRELIABLE_PROCESS_SYNC


def test_refused_database_open_waits_without_spinning(lock_file: str, mocker: MockerFixture) -> None:
    lock = ReadWriteLock(lock_file, is_singleton=False)
    mocker.patch(
        "filelock._sqlite._transactions.connect",
        side_effect=BlockingIOError(EWOULDBLOCK, "leased database"),
        autospec=True,
    )
    started, cpu = time.monotonic(), time.process_time()

    with pytest.raises(Timeout):
        lock.acquire_write(timeout=0.2)

    elapsed = time.monotonic() - started
    assert 0.15 <= elapsed < 1
    assert time.process_time() - cpu < elapsed / 2
    lock.close()


def test_refused_database_open_preserves_deadline_after_a_slow_refusal(lock_file: str, mocker: MockerFixture) -> None:
    lock = ReadWriteLock(lock_file, is_singleton=False)

    now = 0.0
    waits: list[float] = []

    def sleep(seconds: float) -> None:
        nonlocal now
        waits.append(seconds)
        now += seconds

    def refused(_database: str, **_kwargs: float | type[sqlite3.Connection]) -> None:
        nonlocal now
        now += 0.25
        raise BlockingIOError(EWOULDBLOCK, "leased database")

    mocker.patch.object(threading, "active_count", autospec=True, return_value=1)
    clock = mocker.patch("filelock._sqlite._lock.time", autospec=True)
    mocker.patch("filelock._sqlite._transactions.time", clock)
    clock.perf_counter.side_effect = clock.monotonic.side_effect = lambda: now
    clock.sleep.side_effect = sleep
    mocker.patch("filelock._sqlite._transactions.connect", autospec=True, side_effect=refused)

    with pytest.raises(Timeout):
        lock.acquire_write(timeout=0.3)

    assert waits == pytest.approx([0.05])
    lock.close()


@pytest.mark.parametrize(
    "hold_mode",
    [pytest.param("read", id="upgrade"), pytest.param("write", id="downgrade")],
)
@pytest.mark.timeout(PROCESS_DEADLINE * 3)
def test_lock_mode_transition_prohibited(lock_file: str, hold_mode: Literal["read", "write"]) -> None:
    result = Value("i", -1)

    worker = Process(target=try_illegal_transition, args=(lock_file, hold_mode, result))

    with cleanup_processes([worker]):
        worker.start()
        worker.join(timeout=PROCESS_DEADLINE)
        assert not worker.is_alive(), "Process did not exit cleanly"

    assert result.value == 0, f"Illegal {hold_mode} transition was permitted"


def try_illegal_transition(lock_file: str, hold_mode: Literal["read", "write"], result: Synchronized[int]) -> None:
    lock = ReadWriteLock(lock_file)
    with getattr(lock, f"{hold_mode}_lock")():
        try:
            if hold_mode == "read":
                lock.acquire_write()
            else:
                lock.acquire_read()
        except RuntimeError:
            result.value = 0


@pytest.mark.timeout(PROCESS_DEADLINE * 3)
def test_timeout_behavior(lock_file: str) -> None:
    write_acquired = Event()
    release_write = Event()
    read_acquired = Event()

    writer = Process(target=acquire_lock, args=(lock_file, "write", write_acquired, release_write))
    reader = Process(target=acquire_lock, args=(lock_file, "read", read_acquired, None, 0.5, True))

    with cleanup_processes([writer, reader]):
        writer.start()
        assert write_acquired.wait(timeout=PROCESS_DEADLINE), "Write lock not acquired"

        start_time = time.perf_counter()
        reader.start()

        assert not read_acquired.wait(timeout=1), "Read lock should not be acquired"
        reader.join(timeout=PROCESS_DEADLINE)

        elapsed = time.perf_counter() - start_time
        assert 0.4 <= elapsed <= 10.0, f"Timeout was not respected: {elapsed}s"

        release_write.set()
        writer.join(timeout=PROCESS_DEADLINE)


@pytest.mark.timeout(PROCESS_DEADLINE * 3)
def test_non_blocking_behavior(lock_file: str) -> None:
    write_acquired = Event()
    release_write = Event()

    writer = Process(target=acquire_lock, args=(lock_file, "write", write_acquired, release_write))

    with cleanup_processes([writer]):
        writer.start()
        assert write_acquired.wait(timeout=PROCESS_DEADLINE), "Write lock not acquired"

        start_time = time.perf_counter()

        with pytest.raises(Timeout):
            ReadWriteLock(lock_file).acquire_read(blocking=False)

        elapsed = time.perf_counter() - start_time

        assert elapsed < 0.1, f"Non-blocking took too long: {elapsed}s"

        release_write.set()
        writer.join(timeout=PROCESS_DEADLINE)


@pytest.mark.parametrize(
    "mode",
    [pytest.param("read", id="read"), pytest.param("write", id="write")],
)
@pytest.mark.timeout(PROCESS_DEADLINE * 3)
def test_recursive_lock_acquisition(lock_file: str, mode: Literal["read", "write"]) -> None:
    success = Value("i", 0)
    worker = Process(target=recursive_lock, args=(lock_file, mode, success))

    with cleanup_processes([worker]):
        worker.start()
        worker.join(timeout=PROCESS_DEADLINE)

    assert success.value == 1, "Recursive lock acquisition failed"


def recursive_lock(lock_file: str, mode: Literal["read", "write"], success_flag: Synchronized[int]) -> None:
    lock = ReadWriteLock(lock_file)
    acquire = getattr(lock, f"{mode}_lock")
    with acquire():
        assert lock._hold.level == 1
        assert lock._hold.mode == mode

        with acquire():
            assert lock._hold.level == 2
            assert lock._hold.mode == mode

            with acquire():
                assert lock._hold.level == 3
                assert lock._hold.mode == mode

            assert lock._hold.level == 2
            assert lock._hold.mode == mode

        assert lock._hold.level == 1
        assert lock._hold.mode == mode

    assert lock._hold.level == 0
    assert lock._hold.mode is None

    success_flag.value = 1


@pytest.mark.parametrize(
    "mode",
    [pytest.param("read", id="read"), pytest.param("write", id="write")],
)
@pytest.mark.timeout(PROCESS_DEADLINE * 4)
def test_lock_release_on_process_termination(lock_file: str, mode: Literal["read", "write"]) -> None:
    lock_acquired = Event()

    crashing = Process(target=acquire_lock_and_crash, args=(lock_file, mode, lock_acquired))
    crashing.start()

    assert lock_acquired.wait(timeout=PROCESS_DEADLINE), "Lock not acquired by first process"

    write_acquired = Event()
    successor = Process(target=acquire_lock, args=(lock_file, "write", write_acquired))

    # Reap the child before the test runner reports its own timeout.
    reap_deadline: int = 5
    with cleanup_processes([crashing, successor]):
        crashing.terminate()
        crashing.join(timeout=reap_deadline)

        successor.start()

        assert write_acquired.wait(timeout=PROCESS_DEADLINE), "Lock not acquired after process termination"

        successor.join(timeout=PROCESS_DEADLINE)
        assert not successor.is_alive(), "Successor did not exit cleanly"


def acquire_lock_and_crash(lock_file: str, mode: Literal["read", "write"], acquired_event: EventType) -> None:
    lock = ReadWriteLock(lock_file)  # pragma: win32 no cover
    with getattr(lock, f"{mode}_lock")():  # pragma: win32 no cover
        acquired_event.set()
        while True:  # pragma: no cover  # the test kills this child, and the kill can land before this line is traced
            time.sleep(0.1)  # simplint: ignore[sleep-in-test]  # a blocked lock wait can swallow SIGTERM


@pytest.mark.parametrize(
    "mode",
    [pytest.param("read", id="read"), pytest.param("write", id="write")],
)
@pytest.mark.timeout(15)
def test_single_lock_reentrant(lock_file: str, mode: Literal["read", "write"]) -> None:
    acquire = getattr(ReadWriteLock(lock_file), f"{mode}_lock")

    with acquire():
        with acquire():
            pass
        assert not writer_gets_in(lock_file)

    assert writer_gets_in(lock_file)


@pytest.mark.parametrize(
    ("first", "second"),
    [
        pytest.param("read", "write", id="read-then-write"),
        pytest.param("write", "read", id="write-then-read"),
    ],
)
@pytest.mark.timeout(15)
def test_sequential_lock_modes(
    lock_file: str,
    first: Literal["read", "write"],
    second: Literal["read", "write"],
) -> None:
    lock = ReadWriteLock(lock_file)
    for mode in (first, second):
        with getattr(lock, f"{mode}_lock")():
            assert not writer_gets_in(lock_file)
    assert writer_gets_in(lock_file)


def test_read_write_lock_keeps_constructed_database(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    first_directory = tmp_path / "first"
    second_directory = tmp_path / "second"
    first_directory.mkdir()
    second_directory.mkdir()
    first_database = first_directory / "lock.db"
    second_database = second_directory / "lock.db"
    monkeypatch.chdir(first_directory)
    lock = ReadWriteLock("lock.db", is_singleton=False)
    monkeypatch.chdir(second_directory)

    with lock.read_lock():
        assert_read_write_lock_state(str(first_database), "write", available=False)
        assert_read_write_lock_state(str(second_database), "write", available=True)
    lock.close()


@pytest.mark.skipif(not CAPABILITIES["posix-subinterpreters"], reason="needs POSIX subinterpreters")
@pytest.mark.parametrize(
    "without_legacy_module",
    [
        pytest.param(False, id="standard"),
        pytest.param(
            True,
            id="embedded",
            marks=pytest.mark.skipif(not CAPABILITIES["embedded-interpreter-api"], reason="needs the 3.11 C API"),
        ),
    ],
)
def test_read_write_locks_require_the_main_interpreter(
    tmp_path: Path, *, without_legacy_module: bool
) -> None:  # pragma: needs posix-subinterpreters
    interpreters = import_module("_interpreters" if sys.version_info >= (3, 13) else "_xxsubinterpreters")
    child = interpreters.create()
    try:
        child_db = tmp_path / "child.db"
        result = interpreters.run_string(
            child,
            dedent(f"""
            import sys

            if {without_legacy_module!r}:
                sys.modules["_xxsubinterpreters"] = None
            from filelock import AsyncReadWriteLock, ReadWriteLock

            for lock_type in (ReadWriteLock, AsyncReadWriteLock):
                try:
                    lock_type({str(child_db)!r})
                except RuntimeError as error:
                    assert "main interpreter" in str(error), error
                else:
                    raise AssertionError(f"{{lock_type.__name__}} opened a database")
            """),
        )
        assert result is None
        assert not child_db.exists()
        main_db = tmp_path / "main.db"
        with ReadWriteLock(main_db).write_lock():
            assert main_db.is_file()
    finally:
        interpreters.destroy(child)
