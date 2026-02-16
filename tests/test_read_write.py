from __future__ import annotations

import time
from contextlib import contextmanager
from multiprocessing import Event, Process, Value
from typing import TYPE_CHECKING, Literal

import pytest

pytest.importorskip("sqlite3")

from filelock import Timeout
from filelock._read_write import ReadWriteLock

if TYPE_CHECKING:
    from collections.abc import Generator
    from multiprocessing.sharedctypes import Synchronized
    from multiprocessing.synchronize import Event as EventType
    from pathlib import Path


def acquire_lock(
    lock_file: str,
    mode: Literal["read", "write"],
    acquired_event: EventType,
    release_event: EventType | None = None,
    timeout: float = -1,
    blocking: bool = True,
    ready_event: EventType | None = None,
) -> None:
    if ready_event:
        ready_event.wait(timeout=10)

    lock = ReadWriteLock(lock_file, timeout=timeout, blocking=blocking)
    ctx = lock.read_lock() if mode == "read" else lock.write_lock()
    with ctx:
        acquired_event.set()
        if release_event:
            release_event.wait(timeout=10)
        else:
            time.sleep(0.5)  # hold lock briefly to simulate work


@contextmanager
def cleanup_processes(processes: list[Process]) -> Generator[None]:
    try:
        yield
    finally:
        for proc in processes:
            proc.terminate()
            proc.join(timeout=1)


@pytest.fixture
def lock_file(tmp_path: Path) -> str:
    return str(tmp_path / "test_lock.db")


@pytest.mark.timeout(20)
def test_read_locks_are_shared(lock_file: str) -> None:
    """Test that multiple processes can acquire read locks simultaneously."""
    read1_acquired = Event()
    read2_acquired = Event()

    reader1 = Process(target=acquire_lock, args=(lock_file, "read", read1_acquired))
    reader2 = Process(target=acquire_lock, args=(lock_file, "read", read2_acquired))

    with cleanup_processes([reader1, reader2]):
        reader1.start()
        time.sleep(0.1)  # give reader1 time to acquire lock before starting reader2
        reader2.start()

        assert read1_acquired.wait(timeout=2), f"First read lock not acquired on {lock_file}"
        assert read2_acquired.wait(timeout=2), f"Second read lock not acquired on {lock_file}"

        reader1.join(timeout=2)
        reader2.join(timeout=2)
        assert not reader1.is_alive(), "Reader 1 did not exit cleanly"
        assert not reader2.is_alive(), "Reader 2 did not exit cleanly"


@pytest.mark.timeout(20)
def test_write_lock_excludes_other_write_locks(lock_file: str) -> None:
    """Test that a write lock prevents other processes from acquiring write locks."""
    write1_acquired = Event()
    release_write1 = Event()
    write2_acquired = Event()

    holder = Process(target=acquire_lock, args=(lock_file, "write", write1_acquired, release_write1))
    contender = Process(target=acquire_lock, args=(lock_file, "write", write2_acquired, None, 0.5, True))

    with cleanup_processes([holder]):
        holder.start()
        assert write1_acquired.wait(timeout=2), "First write lock not acquired"

        with cleanup_processes([contender]):
            contender.start()
            assert not write2_acquired.wait(timeout=1), "Second write lock should not be acquired"

            release_write1.set()
            holder.join(timeout=2)
            assert not holder.is_alive(), "Holder did not exit cleanly"

        write2_acquired.clear()
        successor = Process(target=acquire_lock, args=(lock_file, "write", write2_acquired))

        with cleanup_processes([successor]):
            successor.start()
            assert write2_acquired.wait(timeout=2), "Second write lock not acquired after first released"
            successor.join(timeout=2)
            assert not successor.is_alive(), "Successor did not exit cleanly"


@pytest.mark.timeout(20)
def test_write_lock_excludes_read_locks(lock_file: str) -> None:
    """Test that a write lock prevents other processes from acquiring read locks."""
    write_acquired = Event()
    release_write = Event()
    read_acquired = Event()
    read_started = Event()

    writer = Process(target=acquire_lock, args=(lock_file, "write", write_acquired, release_write))
    reader = Process(target=acquire_lock, args=(lock_file, "read", read_acquired, None, -1, True, read_started))

    with cleanup_processes([writer, reader]):
        writer.start()
        assert write_acquired.wait(timeout=2), "Write lock not acquired"

        reader.start()
        read_started.set()

        time.sleep(2)  # wait to verify lock is NOT acquired
        assert not read_acquired.is_set(), "Read lock should not be acquired while write lock held"

        release_write.set()
        writer.join(timeout=2)

        assert read_acquired.wait(timeout=2), "Read lock not acquired after write released"

        reader.join(timeout=2)
        assert not reader.is_alive(), "Reader did not exit cleanly"


@pytest.mark.timeout(20)
def test_read_lock_excludes_write_locks(lock_file: str) -> None:
    """Test that read locks prevent other processes from acquiring write locks."""
    read_acquired = Event()
    release_read = Event()
    write_acquired = Event()
    write_started = Event()

    reader = Process(target=acquire_lock, args=(lock_file, "read", read_acquired, release_read))
    writer = Process(target=acquire_lock, args=(lock_file, "write", write_acquired, None, -1, True, write_started))

    with cleanup_processes([reader, writer]):
        reader.start()
        assert read_acquired.wait(timeout=2), "Read lock not acquired"

        writer.start()
        write_started.set()

        time.sleep(2)  # wait to verify lock is NOT acquired
        assert not write_acquired.is_set(), "Write lock should not be acquired while read lock held"

        release_read.set()
        reader.join(timeout=2)

        assert write_acquired.wait(timeout=2), "Write lock not acquired after read released"

        writer.join(timeout=2)
        assert not writer.is_alive(), "Writer did not exit cleanly"


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
            # delay so writer can attempt acquisition while readers overlap
            time.sleep(2)

        if next_reader_signal is not None:
            next_reader_signal.set()

        if reader_index == 0:
            # first reader holds lock briefly then signals writer
            time.sleep(1)

        writer_or_prev_signal.set()

        release_signal.wait(timeout=10)

        with release_count.get_lock():
            release_count.value += 1


@pytest.mark.timeout(40)
def test_write_non_starvation(lock_file: str) -> None:
    """Test that write locks can eventually be acquired even with continuous read locks.

    Creates a chain of reader processes where the writer starts after the first reader acquires a lock. The writer
    should be able to acquire its lock before the entire reader chain has finished, demonstrating non-starvation.

    """
    NUM_READERS = 7

    chain_forward = [Event() for _ in range(NUM_READERS)]
    chain_backward = [Event() for _ in range(NUM_READERS)]
    writer_ready = Event()
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

    writer = Process(target=acquire_lock, args=(lock_file, "write", writer_acquired, None, 20, True, writer_ready))

    with cleanup_processes([*readers, writer]):
        for reader in readers:
            reader.start()

        chain_forward[0].set()

        assert writer_ready.wait(timeout=10), "First reader did not acquire lock"

        writer.start()

        assert writer_acquired.wait(timeout=22), "Writer couldn't acquire lock - possible starvation"

        with release_count.get_lock():
            read_releases = release_count.value

        assert read_releases < 3, f"Writer acquired after {read_releases} readers released - this indicates starvation"

        writer.join(timeout=2)
        assert not writer.is_alive(), "Writer did not exit cleanly"

        for event in chain_backward:
            event.set()

        for idx, reader in enumerate(readers):
            reader.join(timeout=10)
            assert not reader.is_alive(), f"Reader {idx} did not exit cleanly"


def try_upgrade_lock(lock_file: str, upgrade_result: Synchronized[int]) -> None:
    lock = ReadWriteLock(lock_file)
    with lock.read_lock():
        try:
            lock.acquire_write()
        except RuntimeError:
            upgrade_result.value = 0


def try_downgrade_lock(lock_file: str, downgrade_result: Synchronized[int]) -> None:
    lock = ReadWriteLock(lock_file)
    with lock.write_lock():
        try:
            lock.acquire_read()
        except RuntimeError:
            downgrade_result.value = 0


@pytest.mark.timeout(10)
def test_lock_upgrade_prohibited(lock_file: str) -> None:
    """Test that a process cannot upgrade from a read lock to a write lock."""
    upgrade_result = Value("i", -1)

    upgrader = Process(target=try_upgrade_lock, args=(lock_file, upgrade_result))

    with cleanup_processes([upgrader]):
        upgrader.start()
        upgrader.join(timeout=5)
        assert not upgrader.is_alive(), "Process did not exit cleanly"

    assert upgrade_result.value == 0, "Read lock was incorrectly upgraded to write lock"


@pytest.mark.timeout(10)
def test_lock_downgrade_prohibited(lock_file: str) -> None:
    """Test that a process cannot downgrade from a write lock to a read lock."""
    downgrade_result = Value("i", -1)

    downgrader = Process(target=try_downgrade_lock, args=(lock_file, downgrade_result))

    with cleanup_processes([downgrader]):
        downgrader.start()
        downgrader.join(timeout=5)
        assert not downgrader.is_alive(), "Process did not exit cleanly"

    assert downgrade_result.value == 0, "Write lock was incorrectly downgraded to read lock"


@pytest.mark.timeout(10)
def test_timeout_behavior(lock_file: str) -> None:
    """Test that timeout parameter works correctly in multi-process environment."""
    write_acquired = Event()
    release_write = Event()
    read_acquired = Event()

    writer = Process(target=acquire_lock, args=(lock_file, "write", write_acquired, release_write))
    reader = Process(target=acquire_lock, args=(lock_file, "read", read_acquired, None, 0.5, True))

    with cleanup_processes([writer, reader]):
        writer.start()
        assert write_acquired.wait(timeout=2), "Write lock not acquired"

        start_time = time.time()
        reader.start()

        assert not read_acquired.wait(timeout=1), "Read lock should not be acquired"
        reader.join(timeout=5)

        elapsed = time.time() - start_time
        assert 0.4 <= elapsed <= 10.0, f"Timeout was not respected: {elapsed}s"

        release_write.set()
        writer.join(timeout=2)


@pytest.mark.timeout(10)
def test_non_blocking_behavior(lock_file: str) -> None:
    """Test that non-blocking parameter works correctly.

    This test directly attempts to acquire a read lock in non-blocking mode when a write lock is already held by another
    process.

    """
    write_acquired = Event()
    release_write = Event()

    writer = Process(target=acquire_lock, args=(lock_file, "write", write_acquired, release_write))

    with cleanup_processes([writer]):
        writer.start()
        assert write_acquired.wait(timeout=2), "Write lock not acquired"

        lock = ReadWriteLock(lock_file)

        start_time = time.time()

        with pytest.raises(Timeout):
            lock.acquire_read(blocking=False)

        elapsed = time.time() - start_time

        assert elapsed < 0.1, f"Non-blocking took too long: {elapsed}s"

        release_write.set()
        writer.join(timeout=2)


def recursive_read_lock(lock_file: str, success_flag: Synchronized[int]) -> None:
    lock = ReadWriteLock(lock_file)
    with lock.read_lock():
        assert lock._lock_level == 1
        assert lock._current_mode == "read"

        with lock.read_lock():
            assert lock._lock_level == 2
            assert lock._current_mode == "read"

            with lock.read_lock():
                assert lock._lock_level == 3
                assert lock._current_mode == "read"

            assert lock._lock_level == 2
            assert lock._current_mode == "read"

        assert lock._lock_level == 1
        assert lock._current_mode == "read"

    assert lock._lock_level == 0
    assert lock._current_mode is None

    success_flag.value = 1


@pytest.mark.timeout(10)
def test_recursive_read_lock_acquisition(lock_file: str) -> None:
    """Test that the same process can acquire the same read lock multiple times."""
    success = Value("i", 0)
    worker = Process(target=recursive_read_lock, args=(lock_file, success))

    with cleanup_processes([worker]):
        worker.start()
        worker.join(timeout=5)

    assert success.value == 1, "Recursive read lock acquisition failed"


def recursive_write_lock(lock_file: str, success_flag: Synchronized[int]) -> None:
    lock = ReadWriteLock(lock_file)
    with lock.write_lock():
        assert lock._lock_level == 1
        assert lock._current_mode == "write"

        with lock.write_lock():
            assert lock._lock_level == 2
            assert lock._current_mode == "write"

            with lock.write_lock():
                assert lock._lock_level == 3
                assert lock._current_mode == "write"

            assert lock._lock_level == 2
            assert lock._current_mode == "write"

        assert lock._lock_level == 1
        assert lock._current_mode == "write"

    assert lock._lock_level == 0
    assert lock._current_mode is None

    success_flag.value = 1


@pytest.mark.timeout(10)
def test_recursive_write_lock_acquisition(lock_file: str) -> None:
    """Test that the same process can acquire the same write lock multiple times."""
    success = Value("i", 0)
    worker = Process(target=recursive_write_lock, args=(lock_file, success))

    with cleanup_processes([worker]):
        worker.start()
        worker.join(timeout=5)

    assert success.value == 1, "Recursive write lock acquisition failed"


def acquire_write_lock_and_crash(lock_file: str, acquired_event: EventType) -> None:
    lock = ReadWriteLock(lock_file)
    with lock.write_lock():
        acquired_event.set()
        while True:
            time.sleep(0.1)


@pytest.mark.timeout(15)
def test_write_lock_release_on_process_termination(lock_file: str) -> None:
    """Test that write locks are properly released if a process terminates."""
    lock_acquired = Event()

    crashing = Process(target=acquire_write_lock_and_crash, args=(lock_file, lock_acquired))
    crashing.start()

    assert lock_acquired.wait(timeout=2), "Lock not acquired by first process"

    write_acquired = Event()
    successor = Process(target=acquire_lock, args=(lock_file, "write", write_acquired))

    with cleanup_processes([crashing, successor]):
        time.sleep(0.5)  # ensure lock is fully acquired before terminating
        crashing.terminate()
        crashing.join(timeout=2)

        successor.start()

        assert write_acquired.wait(timeout=5), "Lock not acquired after process termination"

        successor.join(timeout=2)
        assert not successor.is_alive(), "Successor did not exit cleanly"


def acquire_read_lock_and_crash(lock_file: str, acquired_event: EventType) -> None:
    lock = ReadWriteLock(lock_file)
    with lock.read_lock():
        acquired_event.set()
        while True:
            time.sleep(0.1)


@pytest.mark.timeout(15)
def test_read_lock_release_on_process_termination(lock_file: str) -> None:
    """Test that readlocks are properly released if a process terminates."""
    lock_acquired = Event()

    crashing = Process(target=acquire_read_lock_and_crash, args=(lock_file, lock_acquired))
    crashing.start()

    assert lock_acquired.wait(timeout=2), "Lock not acquired by first process"

    write_acquired = Event()
    successor = Process(target=acquire_lock, args=(lock_file, "write", write_acquired))

    with cleanup_processes([crashing, successor]):
        time.sleep(0.5)  # ensure lock is fully acquired before terminating
        crashing.terminate()
        crashing.join(timeout=2)

        successor.start()

        assert write_acquired.wait(timeout=5), "Lock not acquired after process termination"

        successor.join(timeout=2)
        assert not successor.is_alive(), "Successor did not exit cleanly"


@pytest.mark.timeout(15)
def test_single_read_lock_acquire_release(lock_file: str) -> None:
    """Test that a single read lock can be acquired and released."""
    lock = ReadWriteLock(lock_file)

    with lock.read_lock(), lock.read_lock():
        pass

    with lock.read_lock():
        pass


@pytest.mark.timeout(15)
def test_single_write_lock_acquire_release(lock_file: str) -> None:
    """Test that a single write lock can be acquired and released."""
    lock = ReadWriteLock(lock_file)

    with lock.write_lock(), lock.write_lock():
        pass

    with lock.write_lock():
        pass


@pytest.mark.timeout(15)
def test_read_then_write_lock(lock_file: str) -> None:
    """Test that we can acquire a read lock and then a write lock after releasing it."""
    lock = ReadWriteLock(lock_file)

    with lock.read_lock():
        pass

    with lock.write_lock():
        pass


@pytest.mark.timeout(15)
def test_write_then_read_lock(lock_file: str) -> None:
    """Test that we can acquire a write lock and then a read lock after releasing it."""
    lock = ReadWriteLock(lock_file)

    with lock.write_lock():
        pass

    with lock.read_lock():
        pass
