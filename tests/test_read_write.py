from __future__ import annotations

import sys
import time
from contextlib import contextmanager
from multiprocessing import Event, Process, Value, set_start_method
from typing import TYPE_CHECKING, Final, Literal, cast

import pytest

pytest.importorskip("sqlite3")

import sqlite3

from filelock import ReadWriteLock, Timeout
from tests.read_write_helpers import assert_read_write_lock_state

# Bounds how long a spawned process may take to reach the lock, not how fast it must be: an interpreter that starts
# slowly under a loaded suite is not a locking failure. Only a genuine hang waits this out. Each test's own timeout
# has to outlast the waits it makes, so those are multiples of this rather than bare seconds.
_PROCESS_DEADLINE: Final[int] = 30

if sys.implementation.name == "pypy":
    set_start_method(
        "spawn", force=True
    )  # pragma: no cover  # exercised only under the pypy fork backend, which runs without coverage

if TYPE_CHECKING:
    from collections.abc import Generator
    from multiprocessing.sharedctypes import Synchronized
    from multiprocessing.synchronize import Event as EventType
    from pathlib import Path

    from pytest_mock import MockerFixture


@pytest.mark.timeout(_PROCESS_DEADLINE * 5)
def test_read_locks_are_shared(lock_file: str) -> None:
    read1_acquired = Event()
    read2_acquired = Event()

    reader1 = Process(target=acquire_lock, args=(lock_file, "read", read1_acquired))
    reader2 = Process(target=acquire_lock, args=(lock_file, "read", read2_acquired))

    with cleanup_processes([reader1, reader2]):
        reader1.start()
        time.sleep(0.5)  # let reader1 acquire before reader2 starts
        reader2.start()

        assert read1_acquired.wait(timeout=10), f"First read lock not acquired on {lock_file}"
        assert read2_acquired.wait(timeout=10), f"Second read lock not acquired on {lock_file}"

        reader1.join(timeout=10)
        reader2.join(timeout=10)
        assert not reader1.is_alive(), "Reader 1 did not exit cleanly"
        assert not reader2.is_alive(), "Reader 2 did not exit cleanly"


@pytest.mark.timeout(_PROCESS_DEADLINE * 5)
def test_write_lock_excludes_other_write_locks(lock_file: str) -> None:
    write1_acquired = Event()
    release_write1 = Event()
    write2_acquired = Event()

    holder = Process(target=acquire_lock, args=(lock_file, "write", write1_acquired, release_write1))
    contender = Process(target=acquire_lock, args=(lock_file, "write", write2_acquired, None, 0.5, True))

    with cleanup_processes([holder]):
        holder.start()
        assert write1_acquired.wait(timeout=_PROCESS_DEADLINE), "First write lock not acquired"

        with cleanup_processes([contender]):
            contender.start()
            assert not write2_acquired.wait(timeout=1), "Second write lock should not be acquired"

            release_write1.set()
            holder.join(timeout=_PROCESS_DEADLINE)
            assert not holder.is_alive(), "Holder did not exit cleanly"

        write2_acquired.clear()
        successor = Process(target=acquire_lock, args=(lock_file, "write", write2_acquired))

        with cleanup_processes([successor]):
            successor.start()
            assert write2_acquired.wait(timeout=_PROCESS_DEADLINE), (
                "Second write lock not acquired after first released"
            )
            successor.join(timeout=_PROCESS_DEADLINE)
            assert not successor.is_alive(), "Successor did not exit cleanly"


@pytest.mark.parametrize(
    ("holder_mode", "contender_mode"),
    [
        pytest.param("write", "read", id="write-blocks-read"),
        pytest.param("read", "write", id="read-blocks-write"),
    ],
)
@pytest.mark.timeout(20)
def test_lock_excludes_opposite_mode(
    lock_file: str,
    holder_mode: Literal["read", "write"],
    contender_mode: Literal["read", "write"],
) -> None:
    holder_acquired = Event()
    release_holder = Event()
    contender_acquired = Event()
    contender_started = Event()

    holder = Process(target=acquire_lock, args=(lock_file, holder_mode, holder_acquired, release_holder))
    contender = Process(
        target=acquire_lock,
        args=(lock_file, contender_mode, contender_acquired, None, -1, True, contender_started),
    )

    with cleanup_processes([holder, contender]):
        holder.start()
        assert holder_acquired.wait(timeout=_PROCESS_DEADLINE), f"{holder_mode} lock not acquired"

        contender.start()
        contender_started.set()

        time.sleep(2)  # confirm the contender stays blocked
        assert not contender_acquired.is_set(), f"{contender_mode} lock should not be acquired while {holder_mode} held"

        release_holder.set()
        holder.join(timeout=_PROCESS_DEADLINE)

        assert contender_acquired.wait(timeout=_PROCESS_DEADLINE), (
            f"{contender_mode} lock not acquired after {holder_mode} released"
        )

        contender.join(timeout=_PROCESS_DEADLINE)
        assert not contender.is_alive(), "Contender did not exit cleanly"


@pytest.mark.timeout(_PROCESS_DEADLINE * 8)
def test_write_non_starvation(lock_file: str) -> None:
    """A writer that joins after the first reader must acquire before the reader chain drains.

    Seven readers overlap in a chain and the writer contends mid-chain. It should win the lock while readers still
    hold it, so it is not starved.
    """
    NUM_READERS: Final[int] = 7

    chain_forward = [Event() for _ in range(NUM_READERS)]
    chain_backward = [Event() for _ in range(NUM_READERS)]
    writer_ready = Event()
    writer_contending = Event()
    writer_acquired = Event()

    release_count = Value("i", 0)

    readers = []
    for idx in range(NUM_READERS):
        next_reader = chain_forward[idx + 1] if idx < NUM_READERS - 1 else None
        prev_or_writer = chain_backward[idx - 1] if idx > 0 else writer_ready
        reader = Process(
            target=chain_reader,
            args=(idx, lock_file, release_count, chain_forward[idx], chain_backward[idx], next_reader, prev_or_writer),
        )
        readers.append(reader)

    writer = Process(
        target=acquire_lock,
        args=(lock_file, "write", writer_acquired, None, 20, True, writer_ready, writer_contending),
    )

    with cleanup_processes([*readers, writer]):
        for reader in readers:
            reader.start()

        chain_forward[0].set()

        assert writer_ready.wait(timeout=_PROCESS_DEADLINE), "First reader did not acquire lock"

        writer.start()

        # Count only the releases the writer actually waited through; a slow interpreter start is not starvation.
        assert writer_contending.wait(timeout=_PROCESS_DEADLINE), "Writer process did not reach the lock"
        with release_count.get_lock():
            releases_before_contending = release_count.value

        assert writer_acquired.wait(timeout=_PROCESS_DEADLINE), "Writer couldn't acquire lock - possible starvation"

        with release_count.get_lock():
            read_releases = release_count.value - releases_before_contending

        assert read_releases < 3, f"Writer acquired after {read_releases} readers released - this indicates starvation"

        writer.join(timeout=_PROCESS_DEADLINE)
        assert not writer.is_alive(), "Writer did not exit cleanly"

        for event in chain_backward:
            event.set()

        for idx, reader in enumerate(readers):
            reader.join(timeout=10)
            assert not reader.is_alive(), f"Reader {idx} did not exit cleanly"


@pytest.mark.parametrize(
    "hold_mode",
    [pytest.param("read", id="upgrade"), pytest.param("write", id="downgrade")],
)
@pytest.mark.timeout(_PROCESS_DEADLINE * 3)
def test_lock_mode_transition_prohibited(lock_file: str, hold_mode: Literal["read", "write"]) -> None:
    result = Value("i", -1)

    worker = Process(target=try_illegal_transition, args=(lock_file, hold_mode, result))

    with cleanup_processes([worker]):
        worker.start()
        worker.join(timeout=5)
        assert not worker.is_alive(), "Process did not exit cleanly"

    assert result.value == 0, f"Illegal {hold_mode} transition was permitted"


@pytest.mark.timeout(_PROCESS_DEADLINE * 3)
def test_timeout_behavior(lock_file: str) -> None:
    write_acquired = Event()
    release_write = Event()
    read_acquired = Event()

    writer = Process(target=acquire_lock, args=(lock_file, "write", write_acquired, release_write))
    reader = Process(target=acquire_lock, args=(lock_file, "read", read_acquired, None, 0.5, True))

    with cleanup_processes([writer, reader]):
        writer.start()
        assert write_acquired.wait(timeout=_PROCESS_DEADLINE), "Write lock not acquired"

        start_time = time.time()
        reader.start()

        assert not read_acquired.wait(timeout=1), "Read lock should not be acquired"
        reader.join(timeout=5)

        elapsed = time.time() - start_time
        assert 0.4 <= elapsed <= 10.0, f"Timeout was not respected: {elapsed}s"

        release_write.set()
        writer.join(timeout=_PROCESS_DEADLINE)


@pytest.mark.timeout(10)
def test_non_blocking_behavior(lock_file: str) -> None:
    write_acquired = Event()
    release_write = Event()

    writer = Process(target=acquire_lock, args=(lock_file, "write", write_acquired, release_write))

    with cleanup_processes([writer]):
        writer.start()
        assert write_acquired.wait(timeout=_PROCESS_DEADLINE), "Write lock not acquired"

        start_time = time.time()

        with pytest.raises(Timeout):
            ReadWriteLock(lock_file).acquire_read(blocking=False)

        elapsed = time.time() - start_time

        assert elapsed < 0.1, f"Non-blocking took too long: {elapsed}s"

        release_write.set()
        writer.join(timeout=_PROCESS_DEADLINE)


@pytest.mark.parametrize(
    "mode",
    [pytest.param("read", id="read"), pytest.param("write", id="write")],
)
@pytest.mark.timeout(10)
def test_recursive_lock_acquisition(lock_file: str, mode: Literal["read", "write"]) -> None:
    success = Value("i", 0)
    worker = Process(target=recursive_lock, args=(lock_file, mode, success))

    with cleanup_processes([worker]):
        worker.start()
        worker.join(timeout=5)

    assert success.value == 1, "Recursive lock acquisition failed"


@pytest.mark.parametrize(
    "mode",
    [pytest.param("read", id="read"), pytest.param("write", id="write")],
)
@pytest.mark.timeout(_PROCESS_DEADLINE * 4)
def test_lock_release_on_process_termination(lock_file: str, mode: Literal["read", "write"]) -> None:
    lock_acquired = Event()

    crashing = Process(target=acquire_lock_and_crash, args=(lock_file, mode, lock_acquired))
    crashing.start()

    assert lock_acquired.wait(timeout=_PROCESS_DEADLINE), "Lock not acquired by first process"

    write_acquired = Event()
    successor = Process(target=acquire_lock, args=(lock_file, "write", write_acquired))

    with cleanup_processes([crashing, successor]):
        time.sleep(0.5)  # let the lock settle before killing the holder
        crashing.terminate()
        crashing.join(timeout=_PROCESS_DEADLINE)

        successor.start()

        assert write_acquired.wait(timeout=5), "Lock not acquired after process termination"

        successor.join(timeout=_PROCESS_DEADLINE)
        assert not successor.is_alive(), "Successor did not exit cleanly"


@pytest.mark.parametrize(
    "mode",
    [pytest.param("read", id="read"), pytest.param("write", id="write")],
)
@pytest.mark.timeout(15)
def test_single_lock_reentrant(lock_file: str, mode: Literal["read", "write"]) -> None:
    lock = ReadWriteLock(lock_file)
    acquire = lock.read_lock if mode == "read" else lock.write_lock

    with acquire(), acquire():
        pass

    with acquire():
        pass


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
        acquire = lock.read_lock if mode == "read" else lock.write_lock
        with acquire():
            pass


@pytest.mark.parametrize(
    "path_change",
    [pytest.param("cwd", id="relative-cwd"), pytest.param("symlink", id="retargeted-symlink")],
)
def test_read_write_lock_keeps_constructed_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, path_change: Literal["cwd", "symlink"]
) -> None:
    first_directory = tmp_path / "first"
    second_directory = tmp_path / "second"
    first_directory.mkdir()
    second_directory.mkdir()
    first_database = first_directory / "lock.db"
    second_database = second_directory / "lock.db"
    if path_change == "cwd":
        monkeypatch.chdir(first_directory)
        lock = ReadWriteLock("lock.db", is_singleton=False)
        monkeypatch.chdir(second_directory)
    else:
        lock_path = tmp_path / "lock.db"
        try:
            lock_path.symlink_to(first_database)
        except OSError as error:  # pragma: no cover - platform policy can deny symlink creation
            pytest.skip(str(error))
        lock = ReadWriteLock(lock_path, is_singleton=False)
        lock_path.unlink()
        lock_path.symlink_to(second_database)

    with lock.read_lock():
        assert_read_write_lock_state(str(first_database), "write", available=False)
        assert_read_write_lock_state(str(second_database), "write", available=True)
    lock.close()


@pytest.mark.parametrize(
    ("held_mode", "probe_mode"),
    [
        pytest.param("read", "write", id="read"),
        pytest.param("write", "read", id="write"),
    ],
)
@pytest.mark.parametrize(
    ("after_rollback", "available_after_failure"),
    [
        pytest.param(False, False, id="transaction-open"),
        pytest.param(True, True, id="transaction-ended"),
    ],
)
def test_release_rollback_failure_reconciles_lock_state(
    lock_file: str,
    mocker: MockerFixture,
    held_mode: Literal["read", "write"],
    probe_mode: Literal["read", "write"],
    *,
    after_rollback: bool,
    available_after_failure: bool,
) -> None:
    rollback_error = _patch_rollback_failure(mocker, after_rollback=after_rollback)
    lock = ReadWriteLock(lock_file, is_singleton=False)
    (lock.acquire_read if held_mode == "read" else lock.acquire_write)()

    with pytest.raises(sqlite3.OperationalError, match="rollback failed") as info:
        lock.release()
    assert info.value is rollback_error
    assert_read_write_lock_state(lock_file, probe_mode, available=available_after_failure)

    if available_after_failure:
        with pytest.raises(RuntimeError, match="not held"):
            lock.release()
    else:
        lock.release()
        assert_read_write_lock_state(lock_file, probe_mode, available=True)
    lock.close()


def _patch_rollback_failure(mocker: MockerFixture, *, after_rollback: bool) -> sqlite3.OperationalError:
    real_connect = sqlite3.connect
    rollback_error = sqlite3.OperationalError("rollback failed")
    connection_count = 0

    def connect(
        database: str,
        *,
        factory: type[sqlite3.Connection],
        timeout: float,
    ) -> sqlite3.Connection:
        nonlocal connection_count
        connection_count += 1
        real_connection = real_connect(
            database,
            check_same_thread=False,
            factory=factory,
            cached_statements=0,
            timeout=timeout,
        )
        if connection_count != 2:
            return real_connection

        rollback_failed = False

        def rollback() -> None:
            nonlocal rollback_failed
            if not rollback_failed:
                rollback_failed = True
                if after_rollback:
                    real_connection.rollback()
                raise rollback_error
            real_connection.rollback()

        connection = mocker.MagicMock(
            spec_set=type(real_connection),
            wraps=real_connection,
            **{"rollback.side_effect": rollback},
        )
        mocker.patch.object(
            type(connection),
            "in_transaction",
            new_callable=mocker.PropertyMock,
            create=True,
            side_effect=lambda: real_connection.in_transaction,
        )
        return cast("sqlite3.Connection", connection)

    mocker.patch("filelock._read_write._connect", side_effect=connect)
    return rollback_error


@pytest.fixture
def lock_file(tmp_path: Path) -> str:
    return str(tmp_path / "test_lock.db")


@pytest.mark.parametrize("mode", [pytest.param("read", id="read"), pytest.param("write", id="write")])
def test_acquire_proxy_releases_the_read_write_lock_on_exit(lock_file: str, mode: Literal["read", "write"]) -> None:
    # acquire_read/acquire_write hand back the shared AcquireReturnProxy; a read/write lock is not a BaseFileLock, so
    # exiting the proxy must route through the lock's own release() rather than a context-error policy.
    lock = ReadWriteLock(lock_file, is_singleton=False)
    acquire = lock.acquire_read if mode == "read" else lock.acquire_write

    with acquire():
        assert_read_write_lock_state(lock_file, "write", available=False)

    assert_read_write_lock_state(lock_file, "write", available=True)


def acquire_lock(
    lock_file: str,
    mode: Literal["read", "write"],
    acquired_event: EventType,
    release_event: EventType | None = None,
    timeout: float = -1,
    blocking: bool = True,
    ready_event: EventType | None = None,
    contending_event: EventType | None = None,
) -> None:
    if ready_event:
        ready_event.wait(timeout=10)

    lock = ReadWriteLock(lock_file, timeout=timeout, blocking=blocking)
    if contending_event:
        contending_event.set()  # interpreter start-up is over, so a caller can time the contention itself
    with lock.read_lock() if mode == "read" else lock.write_lock():
        acquired_event.set()
        if release_event:
            release_event.wait(timeout=10)
        else:
            time.sleep(0.5)  # hold briefly to simulate work


def test_cleanup_reports_the_failure_that_escaped_before_a_start() -> None:
    unstarted = Process(target=time.sleep, args=(0,))

    def fail_before_start() -> None:
        with cleanup_processes([unstarted]):
            msg = "the real failure"
            raise AssertionError(msg)

    with pytest.raises(AssertionError, match="the real failure"):
        fail_before_start()


@contextmanager
def cleanup_processes(processes: list[Process]) -> Generator[None]:
    try:
        yield
    finally:
        for proc in processes:
            # An assertion can escape before every process starts, and terminating an unstarted one raises over the
            # failure that caused it, hiding the real error.
            if proc.pid is not None:
                proc.terminate()
                proc.join(timeout=1)
            proc.close()


def chain_reader(
    reader_index: int,
    lock_file: str,
    release_count: Synchronized[int],
    start_signal: EventType,
    release_signal: EventType,
    next_reader_signal: EventType | None,
    writer_or_prev_signal: EventType,
) -> None:
    start_signal.wait(timeout=10)

    lock = ReadWriteLock(lock_file)
    with lock.read_lock():
        if reader_index > 0:
            time.sleep(2)  # overlap with earlier readers so the writer contends mid-chain

        if next_reader_signal is not None:
            next_reader_signal.set()

        if reader_index == 0:
            time.sleep(1)  # hold briefly before releasing the writer

        writer_or_prev_signal.set()

        release_signal.wait(timeout=10)

        with release_count.get_lock():
            release_count.value += 1


def try_illegal_transition(lock_file: str, hold_mode: Literal["read", "write"], result: Synchronized[int]) -> None:
    lock = ReadWriteLock(lock_file)
    with lock.read_lock() if hold_mode == "read" else lock.write_lock():
        try:
            if hold_mode == "read":
                lock.acquire_write()
            else:
                lock.acquire_read()
        except RuntimeError:
            result.value = 0


def recursive_lock(lock_file: str, mode: Literal["read", "write"], success_flag: Synchronized[int]) -> None:
    lock = ReadWriteLock(lock_file)
    acquire = lock.read_lock if mode == "read" else lock.write_lock
    with acquire():
        assert lock._lock_level == 1
        assert lock._current_mode == mode

        with acquire():
            assert lock._lock_level == 2
            assert lock._current_mode == mode

            with acquire():
                assert lock._lock_level == 3
                assert lock._current_mode == mode

            assert lock._lock_level == 2
            assert lock._current_mode == mode

        assert lock._lock_level == 1
        assert lock._current_mode == mode

    assert lock._lock_level == 0
    assert lock._current_mode is None

    success_flag.value = 1


def acquire_lock_and_crash(lock_file: str, mode: Literal["read", "write"], acquired_event: EventType) -> None:
    lock = ReadWriteLock(lock_file)  # pragma: win32 no cover
    with lock.read_lock() if mode == "read" else lock.write_lock():  # pragma: win32 no cover
        acquired_event.set()
        while True:  # pragma: win32 no cover
            time.sleep(0.1)
