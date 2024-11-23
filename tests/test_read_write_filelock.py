from __future__ import annotations

import threading
import time
from queue import Queue
from typing import TYPE_CHECKING

import pytest

from filelock.read_write import (
    BaseReadWriteFileLockWrapper,
    ReadWriteFileLockWrapper,
    ReadWriteMode,
    has_read_write_lock,
)

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.skipif(not has_read_write_lock, reason="ReadWriteFileLock is not available")
@pytest.mark.parametrize("lock_wrapper_type", [ReadWriteFileLockWrapper])
def test_threaded_read_write_lock(
    lock_wrapper_type: type[BaseReadWriteFileLockWrapper], tmp_path: Path, ex_thread_cls: threading.Thread
) -> None:
    # Runs 100 reader and 10 writer threads.
    # Ensure all readers can acquire the lock at the same time, while no writer can
    # acquire the lock.
    # Release all the readers and ensure only one writer can acquire the lock at the same time.
    read_write_lock = lock_wrapper_type(str(tmp_path / "rw"))

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
