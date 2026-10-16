from __future__ import annotations

import ctypes
import os
import re
import struct
import sys
import time
from contextlib import closing
from multiprocessing import Event, Process, Value, set_start_method
from pathlib import Path
from typing import TYPE_CHECKING, Final, Literal, cast

import pytest
from capabilities import CAPABILITIES

pytest.importorskip("sqlite3")

import sqlite3

from filelock import ReadWriteLock, Timeout
from tests.capability_marks import SKIP_ON_UNRELIABLE_PROCESS_SYNC
from tests.process_helpers import cleanup_processes
from tests.read_write_helpers import assert_read_write_lock_state

# Bounds how long a spawned process may take to reach the lock, not how fast it must be: an interpreter that starts
# slowly under a loaded suite is not a locking failure. Only a genuine hang waits this out. Each test's own timeout
# has to outlast the waits it makes, so those are multiples of this rather than bare seconds.
_PROCESS_DEADLINE: Final[int] = 30
# Reaping a process that has already been signaled is not a startup wait, and it has to stay under the suite's own
# per-test timeout so the guard still reports the hang it was put there for.
_REAP_DEADLINE: Final[int] = 5

pytestmark = SKIP_ON_UNRELIABLE_PROCESS_SYNC

if sys.implementation.name == "pypy":
    set_start_method(
        "spawn", force=True
    )  # pragma: no cover  # exercised only under the pypy fork backend, which runs without coverage

if TYPE_CHECKING:
    from collections.abc import Callable
    from multiprocessing.sharedctypes import Synchronized
    from multiprocessing.synchronize import Event as EventType

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

        assert read1_acquired.wait(timeout=_PROCESS_DEADLINE), f"First read lock not acquired on {lock_file}"
        assert read2_acquired.wait(timeout=_PROCESS_DEADLINE), f"Second read lock not acquired on {lock_file}"

        reader1.join(timeout=_PROCESS_DEADLINE)
        reader2.join(timeout=_PROCESS_DEADLINE)
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
@pytest.mark.timeout(_PROCESS_DEADLINE * 5)
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

    # The last reader holds until its own wait expires, since the test only releases the chain once the writer is in,
    # so the writer has to outwait that hold rather than time out first.
    writer = Process(
        target=acquire_lock,
        args=(lock_file, "write", writer_acquired, None, _PROCESS_DEADLINE * 2, True, writer_ready, writer_contending),
    )

    with cleanup_processes([*readers, writer]):
        for reader in readers:
            reader.start()

        chain_forward[0].set()

        assert writer_ready.wait(timeout=_PROCESS_DEADLINE), "First reader did not acquire lock"

        # Count only the releases the writer actually waited through; a slow interpreter start is not starvation.
        # The contending event says the writer process booted, not that its write intent reached the database, and
        # only the intent bars new readers. The probe observes that boundary: a non-blocking read acquire times out
        # exactly once the intent is registered, so the count starts there. Probing before the writer exists pins
        # the still-pending arc, which the loop alone cannot promise: on a fast host the first in-loop probe
        # already times out.
        prober = ReadWriteLock(lock_file, is_singleton=False)
        probe_deadline = time.monotonic() + _PROCESS_DEADLINE
        assert _write_intent_pending(prober, probe_deadline)

        writer.start()

        assert writer_contending.wait(timeout=_PROCESS_DEADLINE), "Writer process did not reach the lock"
        # pragma-no-branch: the loop never exhausts in a passing run, since the helper's deadline fires first
        for _ in range(_PROCESS_DEADLINE * 20):  # pragma: no branch
            if not _write_intent_pending(prober, probe_deadline):  # pragma: no branch  # exits at the writer's speed
                break
        prober.close()
        with release_count.get_lock():
            releases_before_contending = release_count.value

        assert writer_acquired.wait(timeout=_PROCESS_DEADLINE * 2), "Writer couldn't acquire lock - possible starvation"

        with release_count.get_lock():
            read_releases = release_count.value - releases_before_contending

        assert read_releases < 3, f"Writer acquired after {read_releases} readers released - this indicates starvation"

        writer.join(timeout=_PROCESS_DEADLINE)
        assert not writer.is_alive(), "Writer did not exit cleanly"

        for event in chain_backward:
            event.set()

        for idx, reader in enumerate(readers):
            reader.join(timeout=_PROCESS_DEADLINE)
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
        worker.join(timeout=_PROCESS_DEADLINE)
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
        reader.join(timeout=_PROCESS_DEADLINE)

        elapsed = time.time() - start_time
        assert 0.4 <= elapsed <= 10.0, f"Timeout was not respected: {elapsed}s"

        release_write.set()
        writer.join(timeout=_PROCESS_DEADLINE)


@pytest.mark.timeout(_PROCESS_DEADLINE * 3)
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
@pytest.mark.timeout(_PROCESS_DEADLINE * 3)
def test_recursive_lock_acquisition(lock_file: str, mode: Literal["read", "write"]) -> None:
    success = Value("i", 0)
    worker = Process(target=recursive_lock, args=(lock_file, mode, success))

    with cleanup_processes([worker]):
        worker.start()
        worker.join(timeout=_PROCESS_DEADLINE)

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
        crashing.join(timeout=_REAP_DEADLINE)

        successor.start()

        assert write_acquired.wait(timeout=_PROCESS_DEADLINE), "Lock not acquired after process termination"

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
    # POSIX locks belong to the process, so closing any descriptor on the inode drops every lock it holds there; only a
    # peer process can see that the holder kept its lock.
    lock_file = tmp_path / "lock.db"
    holder = ReadWriteLock(lock_file, is_singleton=False)
    (holder.acquire_read if held_mode == "read" else holder.acquire_write)()
    open_other(lock_file).close()
    assert_read_write_lock_state(str(lock_file), "write", available=False)
    holder.close()


def _hard_link(path: Path) -> Path:  # pragma: needs hard-link
    alias = path.with_name("alias.db")
    os.link(path, alias)
    return alias


@pytest.mark.usefixtures("connect_route")
def test_closing_every_instance_closes_its_descriptors(lock_file: str) -> None:
    if sys.platform == "win32":  # pragma: win32 cover
        pytest.skip("the descriptor table is not listable on Windows")
    descriptor_directory = Path("/proc/self/fd" if sys.platform == "linux" else "/dev/fd")  # pragma: win32 no cover
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
                    not CAPABILITIES["posix-hard-link"],
                    reason="private database aliases need POSIX hard links without following symlinks",
                ),
            ],
        ),
    ]
)
def connect_route(request: pytest.FixtureRequest, tmp_path: Path) -> None:
    # Where others may create names beside the database, SQLite connects through a private link instead of the name.
    if request.param:  # pragma: needs posix-hard-link
        tmp_path.chmod(0o777)


def test_read_write_lock_refuses_a_symlinked_path(tmp_path: Path) -> None:
    # Following it would let anyone who can create names in the lock directory make the victim create the target.
    target = tmp_path / "target.db"
    link = tmp_path / "lock.db"
    try:
        link.symlink_to(target)
    except OSError as error:  # pragma: no cover - platform policy can deny symlink creation
        pytest.skip(str(error))
    # Construction opens a connection, so the refusal lands there; acquiring reopens and refuses the same way.
    with pytest.raises(OSError, match=re.escape(str(link))):
        ReadWriteLock(link, is_singleton=False).acquire_read()
    assert not target.exists()


def test_read_write_lock_refuses_a_non_regular_path(tmp_path: Path) -> None:
    if sys.platform == "win32" or not CAPABILITIES["fifo"]:  # pragma: win32 cover
        pytest.skip("os.mkfifo is unavailable")  # the platform arm also narrows so ty resolves os.mkfifo below
    # A FIFO planted at the lock path opens without blocking (O_NONBLOCK) but is not a database to hand SQLite.
    lock_path = tmp_path / "lock.db"  # pragma: win32 no cover
    os.mkfifo(lock_path)  # pragma: win32 no cover
    with pytest.raises(OSError, match="non-regular"):  # pragma: win32 no cover
        ReadWriteLock(lock_path, is_singleton=False)


def test_read_write_lock_refuses_a_wal_database(lock_file: str) -> None:
    _convert_to_wal(lock_file)
    with pytest.raises(ValueError, match="WAL-mode"):
        ReadWriteLock(lock_file, is_singleton=False)


def test_read_write_lock_refuses_a_database_converted_to_wal_after_construction(lock_file: str) -> None:
    lock = ReadWriteLock(lock_file, is_singleton=False)
    _convert_to_wal(lock_file)
    with pytest.raises(ValueError, match="WAL-mode"):
        lock.acquire_read()
    lock.close()


def _convert_to_wal(path: str) -> None:
    with closing(sqlite3.connect(path)) as connection:
        connection.executescript("PRAGMA journal_mode=WAL; CREATE TABLE application (value);").close()


@pytest.mark.parametrize("replace", [pytest.param(False, id="unlinked"), pytest.param(True, id="replaced")])
def test_read_write_lock_refuses_a_database_moved_after_connect(
    tmp_path: Path, mocker: MockerFixture, *, replace: bool
) -> None:
    if sys.platform == "win32":  # pragma: win32 cover
        pytest.skip("SQLite holds the database open without delete sharing, so its name cannot move")
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
    ) -> sqlite3.Connection:
        connection = real_connect(
            database,
            check_same_thread=check_same_thread,
            factory=factory,
            cached_statements=cached_statements,
            timeout=timeout,
        )
        if replace:
            (replacement := tmp_path / "replacement.db").touch()
            replacement.replace(lock_path)
        else:
            lock_path.unlink()
        return connection

    mocker.patch("sqlite3.connect", side_effect=connect_then_move)  # pragma: win32 no cover
    with pytest.raises(OSError, match="unlinked or replaced"):  # pragma: win32 no cover
        lock.acquire_write()
    lock.close()  # pragma: win32 no cover


def test_read_write_lock_refuses_a_database_held_without_sharing(tmp_path: Path) -> None:
    if sys.platform != "win32":  # pragma: win32 no cover
        pytest.skip("share modes exist only on Windows")  # the platform arm also narrows so ty resolves ctypes.WinDLL
    # Another process holding the database with no sharing blocks the handle that pins its name, so construction fails.
    lock_path = tmp_path / "lock.db"  # pragma: win32 cover
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # pragma: win32 cover
    kernel32.CreateFileW.restype = ctypes.c_void_p  # pragma: win32 cover
    # GENERIC_READ | GENERIC_WRITE, no sharing, CREATE_ALWAYS, FILE_ATTRIBUTE_NORMAL
    handle = kernel32.CreateFileW(str(lock_path), 0xC0000000, 0, None, 2, 0x80, None)  # pragma: win32 cover
    try:  # pragma: win32 cover
        with pytest.raises(PermissionError, match="held without sharing"):
            ReadWriteLock(lock_path, is_singleton=False)
    finally:  # pragma: win32 cover
        kernel32.CloseHandle(ctypes.c_void_p(handle))


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
            database_identity=None,
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
        ready_event.wait(timeout=_PROCESS_DEADLINE)

    lock = ReadWriteLock(lock_file, timeout=timeout, blocking=blocking)
    if contending_event:
        contending_event.set()  # interpreter start-up is over, so a caller can time the contention itself
    with lock.read_lock() if mode == "read" else lock.write_lock():
        acquired_event.set()
        if release_event:
            release_event.wait(timeout=_PROCESS_DEADLINE)
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


def _write_intent_pending(prober: ReadWriteLock, deadline: float) -> bool:
    try:
        prober.acquire_read(blocking=False)
    except Timeout:
        return False
    prober.release()
    assert time.monotonic() < deadline, "Writer never registered its write intent"
    time.sleep(0.05)
    return True


def chain_reader(
    reader_index: int,
    lock_file: str,
    release_count: Synchronized[int],
    start_signal: EventType,
    release_signal: EventType,
    next_reader_signal: EventType | None,
    writer_or_prev_signal: EventType,
) -> None:
    start_signal.wait(timeout=_PROCESS_DEADLINE)

    lock = ReadWriteLock(lock_file)
    with lock.read_lock():
        if reader_index > 0:
            time.sleep(2)  # overlap with earlier readers so the writer contends mid-chain

        if next_reader_signal is not None:
            next_reader_signal.set()

        if reader_index == 0:
            time.sleep(1)  # hold briefly before releasing the writer

        writer_or_prev_signal.set()

        release_signal.wait(timeout=_PROCESS_DEADLINE)

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


@pytest.mark.timeout(60)
@pytest.mark.parametrize("mode", [pytest.param("read", id="read"), pytest.param("write", id="write")])
def test_read_write_lock_refuses_a_schema_that_never_finishes_loading(
    tmp_path: Path, mode: Literal["read", "write"]
) -> None:
    # Every interior page leads to the next one through each of its 660 cells, so the schema has 661**4 rows to scan.
    database = tmp_path / "lock.db"
    pages = [
        _btree_page(
            0x05,  # table interior page
            struct.pack(">IB", number + 1, 1),
            660,
            right_child=struct.pack(">I", number + 1),
            header_at=100 if number == 1 else 0,
        )
        for number in range(1, 5)
    ]
    pages.append(_btree_page(_TABLE_LEAF_PAGE, bytes([6, 1, 6, 0, 0, 0, 0, 0]), 1))
    pages[0][:100] = _file_header(len(pages))
    database.write_bytes(b"".join(pages))
    lock = ReadWriteLock(database, is_singleton=False)

    with pytest.raises(ValueError, match="schema"):
        (lock.acquire_read if mode == "read" else lock.acquire_write)(timeout=1)


@pytest.mark.parametrize("mode", [pytest.param("read", id="read"), pytest.param("write", id="write")])
def test_read_write_lock_refuses_a_schema_row_longer_than_a_lock_needs(
    tmp_path: Path, mode: Literal["read", "write"]
) -> None:
    # The row's text continues on an overflow page that names itself as the next one, so one page of disk reads as a
    # megabyte, far more schema text than a lock database holds and below SQLite's one-billion-byte string limit.
    long_sql_length = 1_000_000
    database = tmp_path / "lock.db"
    statement = b"CREATE TABLE application (value) --"
    columns = [b"table", b"application", b"application", b"\x02"]
    serial_types = [13 + 2 * len(column) for column in columns[:3]] + [1, 13 + 2 * long_sql_length]
    record_header = b"".join(_varint(serial_type) for serial_type in serial_types)
    record_start = _varint(len(record_header) + 1) + record_header + b"".join(columns) + statement
    record_length = len(record_start) + long_sql_length - len(statement)
    # How much of an overflowing record a table leaf keeps on its own page.
    local = (minimum := (_PAGE_SIZE - 12) * 32 // 255 - 23) + (record_length - minimum) % (_PAGE_SIZE - 4)
    cell = _varint(record_length) + _varint(1) + record_start.ljust(local, b"-") + struct.pack(">I", 2)
    leaf = _btree_page(_TABLE_LEAF_PAGE, cell, 1, header_at=100)
    page_count = long_sql_length // _PAGE_SIZE + 2
    leaf[:100] = _file_header(page_count)
    database.write_bytes(leaf + struct.pack(">I", 2).ljust(_PAGE_SIZE, b"-"))
    # SQLite refuses a record longer than the file, which a hole stretches at no cost.
    os.truncate(database, page_count * _PAGE_SIZE)
    lock = ReadWriteLock(database, is_singleton=False)

    with pytest.raises(ValueError, match="schema"):
        (lock.acquire_read if mode == "read" else lock.acquire_write)(timeout=1)


# https://www.sqlite.org/fileformat.html
_PAGE_SIZE: Final[int] = 4096
_TABLE_LEAF_PAGE: Final[int] = 0x0D


def _btree_page(kind: int, cell: bytes, cell_count: int, *, right_child: bytes = b"", header_at: int = 0) -> bytearray:
    page = bytearray(_PAGE_SIZE)
    cell_at = _PAGE_SIZE - len(cell)
    page[cell_at:] = cell
    # Every slot of the cell pointer array names the one cell.
    header = struct.pack(">BHHHB", kind, 0, cell_count, cell_at, 0) + right_child
    pointers = struct.pack(">H", cell_at) * cell_count
    page[header_at : header_at + len(header) + len(pointers)] = header + pointers
    return page


def _file_header(page_count: int) -> bytes:
    # Rollback journal format, a matching change counter and version-valid-for, so SQLite trusts the page count.
    return struct.pack(
        ">16sH6B12I20s2I",
        b"SQLite format 3\0",
        _PAGE_SIZE,
        *(1, 1, 0, 64, 32, 32),
        *(1, page_count, 0, 0, 1, 4, 0, 0, 1, 0, 0, 0),
        b"",
        1,
        3040001,
    )


def _varint(value: int) -> bytes:
    groups = [value & 0x7F]
    while value := value >> 7:
        groups.insert(0, value & 0x7F | 0x80)
    return bytes(groups)


_NEEDS_POSIX_HARD_LINK: Final[pytest.MarkDecorator] = pytest.mark.skipif(
    not CAPABILITIES["posix-hard-link"],
    reason="a shared lock directory gets a private link to the database only where POSIX hard links work",
)


@pytest.mark.requires_hard_links
@_NEEDS_POSIX_HARD_LINK
def test_shared_directory_ignores_a_journal_beside_the_database(  # pragma: needs posix-hard-link
    shared_database: Path, tmp_path: Path
) -> None:
    # SQLite treats a journal beside a non-empty database as an interrupted transaction, and finishing it deletes the
    # super-journal file the journal names.
    named = tmp_path / "notes.txt"
    named.write_text("keep")
    with closing(sqlite3.connect(shared_database)) as connection:
        connection.execute("CREATE TABLE application (value)")
    Path(f"{shared_database}-journal").write_bytes(_hot_journal(named))

    with ReadWriteLock(shared_database, is_singleton=False).read_lock():
        pass

    assert named.read_text() == "keep"


@pytest.mark.requires_hard_links
@_NEEDS_POSIX_HARD_LINK
def test_shared_directory_ignores_a_directory_at_the_wal_name(  # pragma: needs posix-hard-link
    shared_database: Path,
) -> None:
    # SQLite removes a leftover -wal beside an empty database, so a name it cannot remove failed every acquire.
    wal = Path(f"{shared_database}-wal")
    wal.mkdir()
    (wal / "entry").touch()

    with ReadWriteLock(shared_database, is_singleton=False).write_lock():
        assert_read_write_lock_state(str(shared_database), "read", available=False)


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
    ) -> sqlite3.Connection:
        shared_database.unlink()
        shared_database.hardlink_to(kept)
        return connect(
            path,
            check_same_thread=check_same_thread,
            factory=factory,
            cached_statements=cached_statements,
            timeout=timeout,
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
def test_private_directory_under_a_shared_parent_refuses_a_replaced_directory(  # pragma: needs posix-hard-link
    shared_database: Path, mocker: MockerFixture
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
    ) -> sqlite3.Connection:
        private.rename(moved)
        private.mkdir(mode=0o700)
        with closing(connect(private / "lock.db")) as replacement:
            replacement.execute("CREATE TABLE application (value)")
        (private / "lock.db-journal").write_bytes(_hot_journal(moved / "notes.txt"))
        (private / "lock.db-wal").write_bytes(b"\0" * 32)
        before[private] = _entries(private)
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
    # The journal and WAL beside the replacement stay, the file the journal names stays, and no bytes change anywhere.
    assert {directory: _entries(directory) for directory in before} == before
    assert_read_write_lock_state(str(moved / "lock.db"), "write", available=True)


def _hot_journal(super_journal: Path) -> bytes:  # pragma: needs posix-hard-link
    # A journal whose tail names a super-journal makes SQLite unlink that file once it finishes the journal:
    # https://www.sqlite.org/fileformat.html#the_rollback_journal
    name = os.fsencode(super_journal)
    return name + struct.pack(">II", len(name), sum(name)) + bytes.fromhex("d9d505f920a163d7")


def _entries(directory: Path) -> list[tuple[str, bytes | None]]:  # pragma: needs posix-hard-link
    return sorted((path.name, path.read_bytes() if path.is_file() else None) for path in directory.iterdir())


@pytest.fixture
def shared_database(tmp_path: Path) -> Path:  # pragma: needs posix-hard-link
    shared = tmp_path / "shared"
    shared.mkdir()
    shared.chmod(0o777)
    return shared / "lock.db"
