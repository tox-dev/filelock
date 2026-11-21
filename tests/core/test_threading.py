from __future__ import annotations

import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest

from filelock import (
    BaseFileLock,
    FileLock,
    SoftFileLock,
)
from tests.core.thread_helpers import WorkerThread

if TYPE_CHECKING:
    from pathlib import Path


# Coverage serializes those 100 threads on its data lock. Allow for slow Windows runners.
@pytest.mark.timeout(60)
@pytest.mark.parametrize(
    "lock_type",
    [
        pytest.param(FileLock, id="FileLock"),
        pytest.param(
            SoftFileLock,
            id="SoftFileLock",
            marks=pytest.mark.skipif(
                sys.platform == "win32",
                reason=(
                    "SoftFileLock uses file-existence locking — on Windows, unlink can silently fail under heavy "
                    "thread contention (EACCES from antivirus/indexer), orphaning the lock file with no recovery path"
                ),
            ),
        ),
    ],
)
def test_threaded_shared_lock_obj(lock_type: type[BaseFileLock], tmp_path: Path) -> None:
    lock = lock_type(str(tmp_path / "a"))

    holders: list[str] = []
    finished: list[None] = []

    def thread_work() -> None:
        name = threading.current_thread().name
        for _ in range(10):
            with lock:
                holders.append(name)
                # GraalPy handles integer zero and float zero differently.
                time.sleep(0.0)  # give up the GIL so an overlapping holder has a chance to show in the slot
                assert holders == [name]
                assert lock.is_locked
                holders.pop()
                finished.append(None)

    threads = [WorkerThread(target=thread_work, name=f"t{i}") for i in range(100)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(finished) == 100 * 10


@pytest.mark.timeout(60)
@pytest.mark.parametrize(
    "lock_type",
    [
        pytest.param(
            FileLock,
            id="FileLock",
            marks=pytest.mark.skipif(
                sys.platform == "win32" and hasattr(sys, "pypy_version_info"),
                reason="deadlocks randomly",
            ),
        ),
        pytest.param(
            SoftFileLock,
            id="SoftFileLock",
            marks=pytest.mark.skipif(
                sys.platform == "win32", reason="SoftFileLock on Windows has race conditions under heavy threading"
            ),
        ),
    ],
)
def test_threaded_lock_different_lock_obj(lock_type: type[BaseFileLock], tmp_path: Path) -> None:
    holders: list[str] = []
    finished: list[None] = []

    def work(lock: BaseFileLock) -> None:
        name = threading.current_thread().name
        for _ in range(100):
            with lock:
                holders.append(name)
                # GraalPy handles integer zero and float zero differently.
                time.sleep(0.0)  # give up the GIL so an overlapping holder has a chance to show in the slot
                assert holders == [name]
                assert lock.is_locked
                holders.pop()
                finished.append(None)

    lock_path = tmp_path / "a"
    lock_1, lock_2 = lock_type(str(lock_path)), lock_type(str(lock_path))
    threads = [
        (WorkerThread(partial(work, lock_1), f"t1_{i}"), WorkerThread(partial(work, lock_2), f"t2_{i}"))
        for i in range(10)
    ]

    for thread_1, thread_2 in threads:
        thread_1.start()
        thread_2.start()
    for thread_1, thread_2 in threads:
        thread_1.join()
        thread_2.join()

    assert len(finished) == 20 * 100


@pytest.mark.parametrize("lock_type", [FileLock, SoftFileLock])
def test_thrashing_with_thread_pool_passing_lock_to_threads(tmp_path: Path, lock_type: type[BaseFileLock]) -> None:
    def mess_with_file(lock_: BaseFileLock) -> None:
        with lock_:
            _check_file_read_write(txt_file)

    lock_file, txt_file = tmp_path / "test.txt.lock", tmp_path / "test.txt"
    lock = lock_type(lock_file)
    with ThreadPoolExecutor() as executor:
        results = [executor.submit(mess_with_file, lock) for _ in range(100)]
    assert all(r.result() is None for r in results)


def _check_file_read_write(txt_file: Path) -> None:
    for _ in range(3):
        uuid = str(uuid4())
        txt_file.write_text(uuid, encoding="utf-8")
        assert txt_file.read_text(encoding="utf-8") == uuid


@pytest.mark.parametrize("lock_type", [FileLock, SoftFileLock])
def test_thrashing_with_thread_pool_global_lock(tmp_path: Path, lock_type: type[BaseFileLock]) -> None:
    def mess_with_file() -> None:
        with lock:
            _check_file_read_write(txt_file)

    lock_file, txt_file = tmp_path / "test.txt.lock", tmp_path / "test.txt"
    lock = lock_type(lock_file)
    with ThreadPoolExecutor() as executor:
        results = [executor.submit(mess_with_file) for _ in range(100)]

    assert all(r.result() is None for r in results)


@pytest.mark.parametrize("lock_type", [FileLock, SoftFileLock])
def test_thrashing_with_thread_pool_lock_recreated_in_each_thread(
    tmp_path: Path,
    lock_type: type[BaseFileLock],
) -> None:
    def mess_with_file() -> None:
        with lock_type(lock_file):
            _check_file_read_write(txt_file)

    lock_file, txt_file = tmp_path / "test.txt.lock", tmp_path / "test.txt"
    with ThreadPoolExecutor() as executor:
        results = [executor.submit(mess_with_file) for _ in range(100)]

    assert all(r.result() is None for r in results)


@pytest.mark.parametrize("lock_type", [FileLock, SoftFileLock])
def test_lock_can_be_non_thread_local(
    tmp_path: Path,
    lock_type: type[BaseFileLock],
) -> None:
    lock = lock_type(tmp_path / "test.lock", thread_local=False)

    for _ in range(2):
        thread = threading.Thread(target=lock.acquire, kwargs={"timeout": 2})
        thread.start()
        thread.join()

    assert lock.lock_counter == 2

    lock.release(force=True)
