from __future__ import annotations

import threading
import time
from contextlib import closing, suppress
from itertools import count
from multiprocessing import Event, Process
from types import SimpleNamespace
from typing import TYPE_CHECKING, Final, Literal

import pytest

from filelock import AsyncSoftReadWriteLock, SoftReadWriteLock, Timeout
from filelock._soft._read_write import _lock as sync_mod
from filelock._soft._read_write._generations import GenerationLog, Snapshot
from filelock._soft._read_write._storage import OsFiles
from tests.capability_marks import (
    SKIP_ON_UNRELIABLE_PROCESS_SYNC,
)
from tests.process_helpers import cleanup_processes

from .lock_helpers import PROCESS_DEADLINE, holders, make_lock, plant_holder, read_state, wait_until, worker

if TYPE_CHECKING:
    from pytest_mock import MockerFixture

pytestmark: Final[list[pytest.MarkDecorator]] = [
    pytest.mark.requires_hard_links,
    pytest.mark.usefixtures("clear_singletons"),
]


def test_blocking_acquire_without_timeout_waits_for_release(lock_file: str) -> None:
    holder = make_lock(lock_file)
    holder.acquire_write(timeout=2)
    acquired = threading.Event()

    def waiter() -> None:
        contender = make_lock(lock_file)
        try:
            contender.acquire_read(timeout=-1)
            acquired.set()
            contender.release()
        finally:
            contender.close()

    thread = threading.Thread(target=waiter)
    thread.start()
    try:
        assert not acquired.wait(timeout=0.2)
        holder.release()
        assert acquired.wait(timeout=PROCESS_DEADLINE)
    finally:
        thread.join(timeout=PROCESS_DEADLINE)
        holder.close()


@SKIP_ON_UNRELIABLE_PROCESS_SYNC
@pytest.mark.timeout(PROCESS_DEADLINE * 5)
def test_multiple_readers_can_hold_simultaneously(lock_file: str) -> None:
    r1, r2, release = Event(), Event(), Event()
    p1 = Process(target=worker, args=(lock_file, "read", r1, release))
    p2 = Process(target=worker, args=(lock_file, "read", r2, release))
    with cleanup_processes([p1, p2]):
        p1.start()
        p2.start()
        assert r1.wait(timeout=PROCESS_DEADLINE)
        assert r2.wait(timeout=PROCESS_DEADLINE)
        release.set()
        p1.join(timeout=PROCESS_DEADLINE)
        p2.join(timeout=PROCESS_DEADLINE)


@SKIP_ON_UNRELIABLE_PROCESS_SYNC
@pytest.mark.timeout(PROCESS_DEADLINE * 4)
@pytest.mark.parametrize("contender_mode", [pytest.param("write", id="writer"), pytest.param("read", id="reader")])
def test_write_lock_excludes_contender(lock_file: str, contender_mode: Literal["read", "write"]) -> None:
    held, release = Event(), Event()
    contender_acquired = Event()
    writer = Process(target=worker, args=(lock_file, "write", held, release))
    contender = Process(target=worker, args=(lock_file, contender_mode, contender_acquired, None, 0.3, True))
    with cleanup_processes([writer, contender]):
        writer.start()
        assert held.wait(timeout=PROCESS_DEADLINE)
        contender.start()
        assert not contender_acquired.wait(timeout=0.5)
        release.set()
        writer.join(timeout=PROCESS_DEADLINE)
        contender.join(timeout=PROCESS_DEADLINE)


@SKIP_ON_UNRELIABLE_PROCESS_SYNC
@pytest.mark.timeout(PROCESS_DEADLINE * 5)
def test_writer_drains_existing_readers(lock_file: str) -> None:
    r_held, r_release = Event(), Event()
    w_held = Event()
    reader = Process(target=worker, args=(lock_file, "read", r_held, r_release))
    writer = Process(target=worker, args=(lock_file, "write", w_held))
    with cleanup_processes([reader, writer]):
        reader.start()
        assert r_held.wait(timeout=PROCESS_DEADLINE)
        writer.start()
        assert not w_held.wait(timeout=0.5)
        r_release.set()
        reader.join(timeout=PROCESS_DEADLINE)
        assert w_held.wait(timeout=PROCESS_DEADLINE)
        writer.join(timeout=PROCESS_DEADLINE)


@SKIP_ON_UNRELIABLE_PROCESS_SYNC
@pytest.mark.timeout(PROCESS_DEADLINE * 7)
def test_writer_preference_blocks_new_readers(lock_file: str) -> None:
    r1_held, r1_release = Event(), Event()
    w_held, w_release = Event(), Event()
    r2_held = Event()
    reader1 = Process(target=worker, args=(lock_file, "read", r1_held, r1_release))
    writer = Process(target=worker, args=(lock_file, "write", w_held, w_release))
    reader2 = Process(target=worker, args=(lock_file, "read", r2_held, None, 10, True))
    with cleanup_processes([reader1, writer, reader2]):
        reader1.start()
        assert r1_held.wait(timeout=PROCESS_DEADLINE)
        writer.start()
        # Starting reader2 before the writer publishes can reverse their order.
        wait_until(lambda: read_state(lock_file).writer is not None)
        reader2.start()
        assert not r2_held.wait(timeout=0.5)
        r1_release.set()
        assert w_held.wait(timeout=PROCESS_DEADLINE)
        assert not r2_held.wait(timeout=0.3)
        w_release.set()
        assert r2_held.wait(timeout=PROCESS_DEADLINE)
        reader1.join(timeout=PROCESS_DEADLINE)
        writer.join(timeout=PROCESS_DEADLINE)
        reader2.join(timeout=PROCESS_DEADLINE)


@pytest.mark.timeout(PROCESS_DEADLINE * 4)
def test_transaction_lock_timeout_across_threads(lock_file: str) -> None:
    # Keep the peer alive through the timeout to isolate contention on the in-process lock.
    peer = SoftReadWriteLock(
        lock_file,
        is_singleton=False,
        heartbeat_interval=0.1,
        stale_threshold=PROCESS_DEADLINE,
        poll_interval=0.02,
    )
    peer.acquire_write(timeout=2)
    try:
        lock = SoftReadWriteLock(
            lock_file,
            is_singleton=False,
            heartbeat_interval=0.1,
            stale_threshold=PROCESS_DEADLINE,
            poll_interval=0.02,
        )
        try:
            thread_ready = threading.Event()
            release_thread = threading.Event()

            def target_a() -> None:
                thread_ready.set()
                with suppress(Timeout):
                    lock.acquire_write(timeout=2)
                release_thread.wait(timeout=PROCESS_DEADLINE)

            thread_a = threading.Thread(target=target_a)
            thread_a.start()
            try:
                thread_ready.wait(timeout=PROCESS_DEADLINE)
                wait_until(lambda: read_state(lock_file).writer is not None)
                with pytest.raises(Timeout):
                    lock.acquire_write(timeout=0.1)
            finally:
                release_thread.set()
                thread_a.join(timeout=PROCESS_DEADLINE)
        finally:
            lock.close()
    finally:
        peer.release()
        peer.close()


@pytest.mark.timeout(PROCESS_DEADLINE * 3)
def test_two_readers_in_same_process_share_slot(lock_file: str) -> None:
    lock = SoftReadWriteLock(
        lock_file,
        is_singleton=False,
        heartbeat_interval=0.1,
        stale_threshold=0.5,
        poll_interval=0.02,
    )
    try:
        barrier = threading.Barrier(8)
        overlap = threading.Barrier(8)
        arrivals: list[int] = []

        def target() -> None:
            barrier.wait(timeout=PROCESS_DEADLINE)
            with lock.read_lock(timeout=5):
                arrivals.append(overlap.wait(timeout=PROCESS_DEADLINE))

        threads = [threading.Thread(target=target) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=PROCESS_DEADLINE)
        assert sorted(arrivals) == list(range(8))
    finally:
        lock.close()


@SKIP_ON_UNRELIABLE_PROCESS_SYNC
@pytest.mark.timeout(PROCESS_DEADLINE * 3)
def test_timeout_raises(lock_file: str, lock: SoftReadWriteLock) -> None:
    held, release = Event(), Event()
    holder = Process(target=worker, args=(lock_file, "write", held, release))
    with cleanup_processes([holder]):
        holder.start()
        assert held.wait(timeout=PROCESS_DEADLINE)
        with pytest.raises(Timeout):
            lock.acquire_write(timeout=0.3)
        release.set()
        holder.join(timeout=PROCESS_DEADLINE)


@SKIP_ON_UNRELIABLE_PROCESS_SYNC
@pytest.mark.timeout(PROCESS_DEADLINE * 3)
def test_non_blocking_writer_contended_raises(lock_file: str, lock: SoftReadWriteLock) -> None:
    held, release = Event(), Event()
    holder = Process(target=worker, args=(lock_file, "write", held, release))
    with cleanup_processes([holder]):
        holder.start()
        assert held.wait(timeout=PROCESS_DEADLINE)
        with pytest.raises(Timeout):
            lock.acquire_write(timeout=1, blocking=False)
        with pytest.raises(Timeout):
            lock.acquire_read(timeout=1, blocking=False)
        release.set()
        holder.join(timeout=PROCESS_DEADLINE)


def test_writer_evicted_while_draining_queues_behind_its_evictor(
    lock_file: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    reader = make_lock(lock_file, heartbeat_interval=10, stale_threshold=40)
    reader.acquire_read(timeout=2)
    writer = make_lock(lock_file, heartbeat_interval=10, stale_threshold=40)
    swapped = threading.Event()
    peer: list[str] = []

    def hook(_seconds: float) -> None:
        if swapped.is_set():
            raise Timeout(lock_file)
        swapped.set()
        log = GenerationLog(OsFiles(lock_file), lock_file, f"{lock_file}.rw")
        peer.append(plant_holder(lock_file, mode="read"))
        latest = log.latest()
        assert log.commit(Snapshot(generation=latest.generation + 1, writer=peer[0], readers=frozenset()))
        reader.release()

    monkeypatch.setattr(
        sync_mod,
        "time",
        SimpleNamespace(monotonic=time.monotonic, perf_counter=time.perf_counter, sleep=hook),
    )
    try:
        with pytest.raises(Timeout):
            writer.acquire_write(timeout=30)
        assert swapped.is_set()
        assert read_state(lock_file).writer == peer[0]
    finally:
        monkeypatch.setattr(sync_mod, "time", time)
        writer.close()
        reader.close()


@pytest.mark.parametrize("mode", [pytest.param("read", id="read"), pytest.param("write", id="write")])
@pytest.mark.parametrize(
    ("timeout", "blocking"),
    [
        pytest.param(0.02, True, id="deadline"),
        pytest.param(0, True, id="zero-timeout"),
        pytest.param(-1, False, id="nonblocking-unbounded"),
        pytest.param(10, False, id="nonblocking-finite"),
    ],
)
def test_contention_obeys_acquisition_policy(
    lock_file: str, mocker: MockerFixture, mode: Literal["read", "write"], timeout: float, *, blocking: bool
) -> None:
    plant_holder(lock_file, mode="write")
    real_sleep = time.sleep

    def sleep(seconds: float) -> None:
        assert blocking
        assert 0 < seconds <= timeout
        real_sleep(seconds)

    sleep_mock = mocker.create_autospec(time.sleep, side_effect=sleep)
    mocker.patch(
        "filelock._soft._read_write._lock.time",
        new=SimpleNamespace(
            monotonic=time.monotonic,
            perf_counter=mocker.create_autospec(time.perf_counter, side_effect=(reading / 100 for reading in count())),
            sleep=sleep_mock,
        ),
    )
    with closing(SoftReadWriteLock(lock_file, timeout=timeout, blocking=blocking, poll_interval=10)) as lock:
        acquire = getattr(lock, f"acquire_{mode}")
        with pytest.raises(Timeout) as caught:
            acquire()
        assert caught.value.lock_file == lock_file
    assert read_state(lock_file).readers == frozenset()
    assert sleep_mock.call_count == int(blocking and timeout > 0)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", [pytest.param("read", id="read"), pytest.param("write", id="write")])
async def test_async_contention_reports_public_path(lock_file: str, mode: Literal["read", "write"]) -> None:
    plant_holder(lock_file, mode="write")
    lock: Final[AsyncSoftReadWriteLock] = AsyncSoftReadWriteLock(lock_file, timeout=0.02)
    try:
        acquire = getattr(lock, f"acquire_{mode}")
        with pytest.raises(Timeout) as caught:
            await acquire()
        assert caught.value.lock_file == lock_file
    finally:
        await lock.close()


def test_writer_timeout_commits_itself_out(lock_file: str) -> None:
    reader = make_lock(lock_file, heartbeat_interval=10, stale_threshold=40)
    writer = make_lock(lock_file, heartbeat_interval=10, stale_threshold=40, poll_interval=0.01)
    try:
        reader.acquire_read()
        with pytest.raises(Timeout):
            writer.acquire_write(timeout=0.05)
        state = read_state(lock_file)
        assert state.writer is None
        assert len(state.readers) == 1
        assert len(holders(lock_file)) == 1
    finally:
        writer.close()
        reader.close()
