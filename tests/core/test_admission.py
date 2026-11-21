from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from queue import Queue
from typing import TYPE_CHECKING, Final, Literal

import pytest

from filelock import (
    BaseFileLock,
    FileLock,
    SoftFileLease,
    SoftFileLock,
    StrictSoftFileLock,
    Timeout,
)
from tests.core.thread_helpers import THREAD_WAIT

if TYPE_CHECKING:
    from collections.abc import Callable, Generator
    from pathlib import Path


_SHARED_THREADS: Final[int] = 8


@pytest.mark.parametrize(
    "lock_type",
    [
        pytest.param(FileLock, id="native"),
        pytest.param(SoftFileLock, id="soft"),
        pytest.param(SoftFileLease, id="lease"),
        pytest.param(StrictSoftFileLock, id="strict", marks=pytest.mark.requires_hard_links),
    ],
)
@pytest.mark.timeout(0)  # Progress and join waits bound stalls; batch runtime is not a lock guarantee.
def test_shared_instance_concurrent_acquire_release_leaves_lock_free(
    tmp_path: Path, lock_type: type[BaseFileLock]
) -> None:
    lock: Final[BaseFileLock] = lock_type(tmp_path / "a.lock", thread_local=False)
    barrier: Final[threading.Barrier] = threading.Barrier(_SHARED_THREADS, timeout=THREAD_WAIT)
    progress: Final[Queue[None]] = Queue()

    def work() -> None:
        barrier.wait()
        for _ in range(500):
            lock.acquire(timeout=THREAD_WAIT)
            assert lock.is_locked
            lock.release()
            progress.put(None)

    threads: Final[list[threading.Thread]] = [
        threading.Thread(target=work, daemon=True) for _ in range(_SHARED_THREADS)
    ]
    for thread in threads:
        thread.start()
    # Slow runners can exceed a single deadline across 500 iterations.
    for _ in range(_SHARED_THREADS * 500):
        progress.get(timeout=THREAD_WAIT)
    for thread in threads:
        thread.join(THREAD_WAIT)
    assert not [thread for thread in threads if thread.is_alive()]

    assert (lock.lock_counter, lock.is_locked) == (0, False)
    with lock_type(tmp_path / "a.lock", timeout=0):
        pass


def test_shared_lease_marker_names_the_lease_token(tmp_path: Path) -> None:
    marker = tmp_path / "a.lock"
    lease = SoftFileLease(marker, thread_local=False)
    barrier = threading.Barrier(_SHARED_THREADS, timeout=THREAD_WAIT)
    published: list[tuple[str | None, str]] = []

    def work(idx: int) -> None:
        for _ in range(50):
            barrier.wait()
            lease.acquire(timeout=THREAD_WAIT)
            barrier.wait()
            if idx == 0:
                published.append((lease.token, marker.read_text()))
                lease.release(force=True)
            barrier.wait()

    _run_shared_threads(work)

    assert [(token, content) for token, content in published if f"token={token}\n" not in content] == []


def _run_shared_threads(target: Callable[[int], None]) -> None:
    # Pytest treats unhandled worker exceptions as errors.
    threads = [threading.Thread(target=target, args=(idx,), daemon=True) for idx in range(_SHARED_THREADS)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(THREAD_WAIT)
    assert not [thread for thread in threads if thread.is_alive()]


def test_shared_instance_released_on_another_thread_frees_the_acquirer(tmp_path: Path) -> None:
    shared = FileLock(tmp_path / "a.lock", thread_local=False)

    def acquire_fresh() -> bool:
        with FileLock(tmp_path / "a.lock") as fresh:
            return fresh.is_locked

    with ThreadPoolExecutor(max_workers=1) as acquirer:
        acquirer.submit(shared.acquire).result(THREAD_WAIT)
        shared.release()

        assert not shared.is_locked
        assert acquirer.submit(acquire_fresh).result(THREAD_WAIT)


def test_shared_instance_release_behind_a_failed_acquire_is_a_no_op(tmp_path: Path) -> None:
    releasing = threading.Event()

    def release_from_another_thread() -> None:
        releasing.set()
        lock.release()

    releaser = threading.Thread(target=release_from_another_thread, daemon=True)

    def fail_after_starting_release(_: int) -> None:
        releaser.start()
        releasing.wait(THREAD_WAIT)
        # Give the releaser time to wait on this transition before rollback frees the lock.
        releaser.join(0.2)
        msg = "hook failed"
        raise RuntimeError(msg)

    lock = FileLock(tmp_path / "a.lock", thread_local=False, on_acquired=fail_after_starting_release)
    with pytest.raises(RuntimeError, match="hook failed"):
        lock.acquire(timeout=THREAD_WAIT)
    releaser.join(THREAD_WAIT)

    assert (releaser.is_alive(), lock.lock_counter, lock.is_locked) == (False, 0, False)


@pytest.fixture
def paused_in_transition(tmp_path: Path) -> Generator[FileLock]:
    inside, resume = threading.Event(), threading.Event()

    def pause(_: int) -> None:
        inside.set()
        resume.wait(THREAD_WAIT)

    lock = FileLock(tmp_path / "a.lock", thread_local=False, on_acquired=pause)
    holder = threading.Thread(target=lock.acquire, kwargs={"timeout": THREAD_WAIT}, daemon=True)
    holder.start()
    assert inside.wait(THREAD_WAIT)
    yield lock
    resume.set()
    holder.join(THREAD_WAIT)
    assert not holder.is_alive()
    lock.release(force=True)


@pytest.mark.parametrize("admission", ["timeout", "blocking", "cancel"])
def test_shared_instance_refuses_entry_during_another_threads_transition(
    paused_in_transition: FileLock, admission: Literal["timeout", "blocking", "cancel"]
) -> None:
    with pytest.raises(Timeout):
        _acquire_shared_with(paused_in_transition, admission)

    assert paused_in_transition.lock_counter == 1


def _acquire_shared_with(lock: FileLock, admission: Literal["timeout", "blocking", "cancel"]) -> None:
    if admission == "timeout":
        lock.acquire(timeout=0)
    elif admission == "blocking":
        lock.acquire(blocking=False)
    else:
        lock.acquire(cancel_check=lambda: True)


def test_shared_instance_waiter_enters_once_the_transition_finishes(tmp_path: Path) -> None:
    inside, resume = threading.Event(), threading.Event()

    def pause(_: int) -> None:
        inside.set()
        resume.wait(THREAD_WAIT)

    lock = FileLock(tmp_path / "a.lock", thread_local=False, on_acquired=pause)
    holder = threading.Thread(target=lock.acquire, kwargs={"timeout": THREAD_WAIT}, daemon=True)
    holder.start()
    assert inside.wait(THREAD_WAIT)
    # The waiter must survive several admission polls before the holder leaves.
    threading.Timer(0.1, resume.set).start()

    lock.acquire(timeout=THREAD_WAIT, poll_interval=0.01)
    holder.join(THREAD_WAIT)

    assert (holder.is_alive(), lock.lock_counter) == (False, 2)
    lock.release(force=True)


def test_shared_instance_admission_wait_respects_the_caller_timeout(paused_in_transition: FileLock) -> None:
    start: Final[float] = time.perf_counter()
    with pytest.raises(Timeout):
        paused_in_transition.acquire(timeout=0.05, poll_interval=THREAD_WAIT)
    assert time.perf_counter() - start < THREAD_WAIT / 2
