from __future__ import annotations

import asyncio
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from queue import Queue
from typing import TYPE_CHECKING

import pytest

from filelock import Timeout
from filelock.read_write import (
    AsyncReadWriteFileLockWrapper,
    ReadWriteFileLockWrapper,
    ReadWriteMode,
    has_read_write_file_lock,
)

if TYPE_CHECKING:
    from pathlib import Path

if not has_read_write_file_lock:
    pytest.skip(reason="ReadWriteFileLock is not available", allow_module_level=True)


def test_basic_read_lock(tmp_path: Path) -> None:
    rw_wrapper = ReadWriteFileLockWrapper(str(tmp_path / "test_rw"))
    with rw_wrapper.read() as lock:
        assert lock.is_locked
        assert lock.read_write_mode == ReadWriteMode.READ
    assert not lock.is_locked


def test_basic_write_lock(tmp_path: Path) -> None:
    rw_wrapper = ReadWriteFileLockWrapper(str(tmp_path / "test_rw"))
    with rw_wrapper.write() as lock:
        assert lock.is_locked
        assert lock.read_write_mode == ReadWriteMode.WRITE
    assert not lock.is_locked


def test_reentrant_read_lock(tmp_path: Path) -> None:
    rw_wrapper = ReadWriteFileLockWrapper(str(tmp_path / "test_rw"))
    lock = rw_wrapper.read()
    with lock:
        assert lock.is_locked
        with lock:
            assert lock.is_locked
        assert lock.is_locked
    assert not lock.is_locked


def test_reentrant_write_lock(tmp_path: Path) -> None:
    rw_wrapper = ReadWriteFileLockWrapper(str(tmp_path / "test_rw"))
    lock = rw_wrapper.write()
    with lock:
        assert lock.is_locked
        with lock:
            assert lock.is_locked
        assert lock.is_locked
    assert not lock.is_locked


def test_multiple_readers_shared_lock(tmp_path: Path) -> None:
    rw_wrapper = ReadWriteFileLockWrapper(str(tmp_path / "test_rw"))
    lock1 = rw_wrapper.read()
    lock2 = rw_wrapper.read()

    with lock1:
        assert lock1.is_locked
        # Acquiring another read lock should not block
        with lock2:
            assert lock2.is_locked
        assert lock1.is_locked
    assert not lock1.is_locked
    assert not lock2.is_locked


def test_writer_excludes_readers(tmp_path: Path) -> None:
    rw_wrapper = ReadWriteFileLockWrapper(str(tmp_path / "test_rw"))
    rlock = rw_wrapper.read()
    wlock = rw_wrapper.write()

    with wlock:
        assert wlock.is_locked
        # Attempting to acquire a read lock now should block or time out
        start = time.perf_counter()
        with pytest.raises((Timeout, Exception)):
            rlock.acquire(timeout=0.1, blocking=False)
        end = time.perf_counter()
        assert (end - start) < 1.0  # ensure it didn't block too long
    assert not wlock.is_locked


def test_readers_blocked_by_writer_priority(tmp_path: Path, ex_thread_cls: threading.Thread) -> None:
    """
    Once a writer is waiting for the lock, new readers should not enter.
    This ensures writer preference.
    """
    rw_wrapper = ReadWriteFileLockWrapper(str(tmp_path / "test_rw"))
    rlock = rw_wrapper.read()
    wlock = rw_wrapper.write()

    # Acquire read lock first
    with rlock:
        # Start a writer in another thread and ensure it wants the lock
        def writer() -> None:
            with wlock:
                pass

        t = ex_thread_cls(target=writer, name="writer")
        t.start()

        # Give some time for writer to start and attempt acquire
        time.sleep(0.1)

        # Now attempt to acquire another read lock - should block or timeout because writer preference
        another_r = rw_wrapper.read()
        with pytest.raises((Timeout, Exception)):
            another_r.acquire(timeout=0.1, blocking=True)

    # Now that the read lock is released, the writer should get the lock and release it
    t.join()
    assert not rlock.is_locked
    assert not wlock.is_locked


def test_non_blocking_read_when_write_held(tmp_path: Path) -> None:
    rw_wrapper = ReadWriteFileLockWrapper(str(tmp_path / "test_rw"))
    wlock = rw_wrapper.write()
    rlock = rw_wrapper.read()

    wlock.acquire()
    assert wlock.is_locked
    # Non-blocking read should fail immediately
    with pytest.raises((Timeout, Exception)):
        rlock.acquire(blocking=False)
    wlock.release()


def test_timeout_read_lock(tmp_path: Path) -> None:
    rw_wrapper = ReadWriteFileLockWrapper(str(tmp_path / "test_rw"))
    wlock = rw_wrapper.write()
    rlock = rw_wrapper.read()

    wlock.acquire()
    with pytest.raises((Timeout, Exception)):
        # Attempt read lock with some timeout
        rlock.acquire(timeout=0.1)
    wlock.release()


def test_timeout_write_lock(tmp_path: Path) -> None:
    rw_wrapper = ReadWriteFileLockWrapper(str(tmp_path / "test_rw"))
    rlock = rw_wrapper.read()
    wlock = rw_wrapper.write()

    # Acquire a read lock first
    rlock.acquire()
    assert rlock.is_locked
    # Attempt to acquire a write lock with a short timeout
    with pytest.raises((Timeout, Exception)):
        wlock.acquire(timeout=0.1)
    rlock.release()


def test_forced_release(tmp_path: Path) -> None:
    rw_wrapper = ReadWriteFileLockWrapper(str(tmp_path / "test_rw"))
    rlock = rw_wrapper.read()
    rlock.acquire()
    assert rlock.is_locked

    # Force release
    rlock.release(force=True)
    assert not rlock.is_locked


def test_stress_multiple_threads_readers_and_writers(tmp_path: Path, ex_thread_cls: threading.Thread) -> None:
    # Stress test: multiple readers and writers competing
    rw_wrapper = ReadWriteFileLockWrapper(str(tmp_path / "test_rw"))
    num_readers = 50
    num_writers = 10

    read_hold_time = 0.01
    write_hold_time = 0.02

    # Shared resource
    shared_data = []
    shared_data_lock = threading.Lock()

    def reader() -> None:
        with rw_wrapper.read():
            # Multiple readers can enter
            # Just check that no writes happen simultaneously
            time.sleep(read_hold_time)
            with shared_data_lock:
                # Check consistency
                pass

    def writer() -> None:
        with rw_wrapper.write():
            # Exclusive access
            old_len = len(shared_data)
            time.sleep(write_hold_time)
            with shared_data_lock:
                shared_data.append(1)
                assert len(shared_data) == old_len + 1

    threads = []
    threads.extend(ex_thread_cls(target=reader, name=f"reader_{i}") for i in range(num_readers))
    threads.extend(ex_thread_cls(target=writer, name=f"writer_{i}") for i in range(num_writers))

    # Shuffle threads to randomize execution
    random.shuffle(threads)

    for t in threads:
        t.start()

    for t in threads:
        t.join()

    # We expect that all writers appended to the list
    assert len(shared_data) == num_writers


def test_thrashing_with_thread_pool_readers_writers(tmp_path: Path) -> None:
    rw_wrapper = ReadWriteFileLockWrapper(str(tmp_path / "test_rw"))

    txt_file = tmp_path / "data.txt"
    txt_file.write_text("initial")

    def read_work() -> None:
        with rw_wrapper.read():
            txt_file.read_text()
            time.sleep(0.001)

    def write_work() -> None:
        with rw_wrapper.write():
            current = txt_file.read_text()
            txt_file.write_text(current + "x")
            time.sleep(0.002)

    with ThreadPoolExecutor() as executor:
        futures = []
        futures.extend(executor.submit(read_work) for _ in range(50))
        futures.extend(executor.submit(write_work) for _ in range(10))

        # Add more mixed load
        for _ in range(20):
            futures.append(executor.submit(read_work))  # noqa: FURB113
            futures.append(executor.submit(write_work))

        for f in futures:
            f.result()

    # Ensure file got appended by writers
    final_data = txt_file.read_text()
    # At least writers appended something
    assert len(final_data) > len("initial")


def test_threaded_read_write_lock(tmp_path: Path, ex_thread_cls: threading.Thread) -> None:
    # Runs 100 reader and 10 writer threads.
    # Ensure all readers can acquire the lock at the same time, while no writer can
    # acquire the lock.
    # Release all the readers and ensure only one writer can acquire the lock at the same time.

    # Note that we can do this because ReadWriteFileLock is thread local by default.
    read_write_lock = ReadWriteFileLockWrapper(str(tmp_path / "rw"))

    num_readers = 0
    num_readers_lock = threading.Lock()
    num_writers = 0
    num_writers_lock = threading.Lock()
    is_ready_readers_queue = Queue()
    is_ready_writers_queue = Queue()
    should_proceed_event = threading.Event()
    is_writer_ready_event = threading.Event()

    def read_thread_work() -> None:
        nonlocal num_readers
        with read_write_lock(ReadWriteMode.READ):
            assert read_write_lock(ReadWriteMode.READ).is_locked
            with num_readers_lock:
                num_readers += 1
            is_ready_readers_queue.put_nowait(None)
            should_proceed_event.wait()
            with num_readers_lock:
                num_readers -= 1

    def write_thread_work() -> None:
        nonlocal num_writers
        is_writer_ready_event.set()
        with read_write_lock(ReadWriteMode.WRITE):
            assert read_write_lock(ReadWriteMode.WRITE).is_locked
            with num_writers_lock:
                num_writers += 1
            is_ready_writers_queue.put(1)
            with num_writers_lock:
                num_writers -= 1

    read_threads = [ex_thread_cls(target=read_thread_work, name=f"rt{i}") for i in range(100)]
    for thread in read_threads:
        thread.start()

    for _ in read_threads:
        is_ready_readers_queue.get()

    with num_readers_lock:
        assert num_readers == len(read_threads)

    write_threads = [ex_thread_cls(target=write_thread_work, name=f"wt{i}") for i in range(10)]
    for thread in write_threads:
        thread.start()

    is_writer_ready_event.wait()

    # Sleeps are not ideal...
    time.sleep(0.1)
    with num_writers_lock:
        assert num_writers == 0
    time.sleep(0.1)

    should_proceed_event.set()

    for _ in write_threads:
        is_ready_writers_queue.get()
        with num_writers_lock:
            assert num_writers in {0, 1}

    for thread in write_threads:
        thread.join()

    assert not read_write_lock(ReadWriteMode.READ).is_locked
    assert not read_write_lock(ReadWriteMode.WRITE).is_locked
    assert num_readers == 0
    assert num_writers == 0


@pytest.mark.asyncio
async def test_async_basic_read_lock(tmp_path: Path) -> None:
    rw_wrapper = AsyncReadWriteFileLockWrapper(lock_file=str(tmp_path / "test_rw"))
    async with rw_wrapper.read() as lock:
        assert lock.is_locked
        assert lock.read_write_mode == ReadWriteMode.READ
    assert not lock.is_locked


@pytest.mark.asyncio
async def test_async_basic_write_lock(tmp_path: Path) -> None:
    rw_wrapper = AsyncReadWriteFileLockWrapper(lock_file=str(tmp_path / "test_rw"))
    async with rw_wrapper.write() as lock:
        assert lock.is_locked
        assert lock.read_write_mode == ReadWriteMode.WRITE
    assert not lock.is_locked


@pytest.mark.asyncio
async def test_async_reentrant_read_lock(tmp_path: Path) -> None:
    rw_wrapper = AsyncReadWriteFileLockWrapper(lock_file=str(tmp_path / "test_rw"))
    lock = rw_wrapper.read()
    async with lock:
        assert lock.is_locked
        async with lock:
            assert lock.is_locked
        assert lock.is_locked
    assert not lock.is_locked


@pytest.mark.asyncio
async def test_async_reentrant_write_lock(tmp_path: Path) -> None:
    rw_wrapper = AsyncReadWriteFileLockWrapper(lock_file=str(tmp_path / "test_rw"))
    lock = rw_wrapper.write()
    async with lock:
        assert lock.is_locked
        async with lock:
            assert lock.is_locked
        assert lock.is_locked
    assert not lock.is_locked


@pytest.mark.asyncio
async def test_async_multiple_readers_shared_lock(tmp_path: Path) -> None:
    rw_wrapper = AsyncReadWriteFileLockWrapper(lock_file=str(tmp_path / "test_rw"))
    lock1 = rw_wrapper.read()
    lock2 = rw_wrapper.read()

    async with lock1:
        assert lock1.is_locked
        # Another read lock should also be acquirable without blocking
        async with lock2:
            assert lock2.is_locked
        assert lock1.is_locked
    assert not lock1.is_locked
    assert not lock2.is_locked


@pytest.mark.asyncio
async def test_async_writer_excludes_readers(tmp_path: Path) -> None:
    rw_wrapper = AsyncReadWriteFileLockWrapper(lock_file=str(tmp_path / "test_rw"))
    rlock = rw_wrapper.read()
    wlock = rw_wrapper.write()

    async with wlock:
        assert wlock.is_locked
        # Attempting to acquire read lock should fail immediately if non-blocking
        with pytest.raises(Timeout):
            await rlock.acquire(timeout=0.1, blocking=False)
    assert not wlock.is_locked


@pytest.mark.asyncio
async def test_async_readers_blocked_by_writer_priority(tmp_path: Path) -> None:
    """
    Once a writer is waiting for the lock, new readers should not start.
    This ensures writer preference.
    """
    rw_wrapper = AsyncReadWriteFileLockWrapper(lock_file=str(tmp_path / "test_rw"))
    rlock = rw_wrapper.read()
    wlock = rw_wrapper.write()

    await rlock.acquire()
    try:
        # Start a writer attempt in another task
        async def writer() -> None:
            async with wlock:
                pass

        writer_task = asyncio.create_task(writer())
        # Give the writer a moment to start and attempt acquire
        await asyncio.sleep(0.1)

        # Now attempt another read lock - should fail due to writer preference
        another_r = rw_wrapper.read()
        with pytest.raises(Timeout):
            await another_r.acquire(timeout=0.1, blocking=True)

    finally:
        await rlock.release()

    # Once read lock is released, writer should proceed
    await writer_task


@pytest.mark.asyncio
async def test_async_non_blocking_read_when_write_held(tmp_path: Path) -> None:
    rw_wrapper = AsyncReadWriteFileLockWrapper(lock_file=str(tmp_path / "test_rw"))
    wlock = rw_wrapper.write()
    rlock = rw_wrapper.read()

    await wlock.acquire()
    try:
        with pytest.raises(Timeout):
            await rlock.acquire(blocking=False)
    finally:
        await wlock.release()


@pytest.mark.asyncio
async def test_async_timeout_read_lock(tmp_path: Path) -> None:
    rw_wrapper = AsyncReadWriteFileLockWrapper(lock_file=str(tmp_path / "test_rw"))
    wlock = rw_wrapper.write()
    rlock = rw_wrapper.read()

    await wlock.acquire()
    try:
        with pytest.raises(Timeout):
            await rlock.acquire(timeout=0.1)
    finally:
        await wlock.release()


@pytest.mark.asyncio
async def test_async_timeout_write_lock(tmp_path: Path) -> None:
    rw_wrapper = AsyncReadWriteFileLockWrapper(lock_file=str(tmp_path / "test_rw"))
    rlock = rw_wrapper.read()
    wlock = rw_wrapper.write()

    await rlock.acquire()
    try:
        with pytest.raises(Timeout):
            await wlock.acquire(timeout=0.1)
    finally:
        await rlock.release()


@pytest.mark.asyncio
async def test_async_forced_release(tmp_path: Path) -> None:
    rw_wrapper = AsyncReadWriteFileLockWrapper(lock_file=str(tmp_path / "test_rw"))
    rlock = rw_wrapper.read()
    await rlock.acquire()
    assert rlock.is_locked
    await rlock.release(force=True)
    assert not rlock.is_locked


@pytest.mark.asyncio
async def test_async_stress_multiple_tasks_readers_and_writers(tmp_path: Path) -> None:
    num_readers = 50
    num_writers = 10

    # Shared state
    shared_data = []
    data_lock = asyncio.Lock()

    async def reader() -> None:
        rw_wrapper = AsyncReadWriteFileLockWrapper(lock_file=str(tmp_path / "test_rw"))
        async with rw_wrapper.read():
            # Multiple readers allowed
            await asyncio.sleep(0.01)
            # Just check/inspect shared_data under lock
            async with data_lock:
                # read operation
                pass

    async def writer() -> None:
        rw_wrapper = AsyncReadWriteFileLockWrapper(lock_file=str(tmp_path / "test_rw"))
        async with rw_wrapper.write():
            async with data_lock:
                old_len = len(shared_data)
            await asyncio.sleep(0.02)
            async with data_lock:
                shared_data.append(1)
                assert len(shared_data) == old_len + 1

    tasks = []
    tasks.extend(asyncio.create_task(reader()) for _ in range(num_readers))
    tasks.extend(asyncio.create_task(writer()) for _ in range(num_writers))

    # Shuffle tasks
    random.shuffle(tasks)
    await asyncio.gather(*tasks)

    # All writers must have appended to shared_data
    assert len(shared_data) == num_writers


@pytest.mark.asyncio
async def test_async_asyncio_concurrent_readers_writers(tmp_path: Path) -> None:
    # Similar stress test with mixed load
    txt_file = tmp_path / "data.txt"
    txt_file.write_text("initial")

    async def read_work() -> None:
        rw_wrapper = AsyncReadWriteFileLockWrapper(lock_file=str(tmp_path / "test_rw"))
        async with rw_wrapper.read():
            _ = txt_file.read_text()
            await asyncio.sleep(0.001)

    async def write_work() -> None:
        rw_wrapper = AsyncReadWriteFileLockWrapper(lock_file=str(tmp_path / "test_rw"))
        async with rw_wrapper.write():
            current = txt_file.read_text()
            txt_file.write_text(current + "x")
            await asyncio.sleep(0.002)

    tasks = []
    tasks.extend(asyncio.create_task(read_work()) for _ in range(50))
    tasks.extend(asyncio.create_task(write_work()) for _ in range(10))

    # Add more mixed load
    for _ in range(20):
        tasks.append(asyncio.create_task(read_work()))  # noqa: FURB113
        tasks.append(asyncio.create_task(write_work()))

    await asyncio.gather(*tasks)

    final_data = txt_file.read_text()
    # At least some writes have occurred
    assert len(final_data) > len("initial")


def test_async_cannot_use_with_instead_of_async_with(tmp_path: Path) -> None:
    rw_wrapper = AsyncReadWriteFileLockWrapper(lock_file=str(tmp_path / "test_rw"))
    # Trying to use sync `with` should raise NotImplementedError
    lock = rw_wrapper.read()
    with pytest.raises(NotImplementedError, match="Do not use `with` for asyncio locks"), lock:
        pass

    lock = rw_wrapper.write()
    with pytest.raises(NotImplementedError, match="Do not use `with` for asyncio locks"), lock:
        pass


@pytest.mark.asyncio
async def test_async_writer_priority_race_condition(tmp_path: Path) -> None:
    """
    Tests a scenario where multiple tasks attempt to read and write concurrently,
    to ensure writer preference is maintained.
    """

    # Track how many readers and writers are active
    active_readers = 0
    active_writers = 0
    lock = asyncio.Lock()  # to guard active_readers & active_writers

    async def reader() -> None:
        nonlocal active_readers, active_writers
        rw_wrapper = AsyncReadWriteFileLockWrapper(lock_file=str(tmp_path / "test_rw"))
        read_lock = rw_wrapper.read()
        async with read_lock:
            async with lock:
                active_readers += 1
                # No writer should be active while reading
                assert active_writers == 0
            await asyncio.sleep(0.001)
            async with lock:
                active_readers -= 1

    async def writer() -> None:
        nonlocal active_readers, active_writers
        rw_wrapper = AsyncReadWriteFileLockWrapper(lock_file=str(tmp_path / "test_rw"))
        write_lock = rw_wrapper.write()
        async with write_lock:
            async with lock:
                active_writers += 1
                # No readers should be active while writing
                assert active_readers == 0
            await asyncio.sleep(0.002)
            async with lock:
                active_writers -= 1

    tasks = []
    tasks.extend(asyncio.create_task(reader()) for _ in range(5))
    tasks.extend(asyncio.create_task(writer()) for _ in range(5))

    # Shuffle tasks
    random.shuffle(tasks)

    await asyncio.gather(*tasks)

    assert active_readers == 0
    assert active_writers == 0
