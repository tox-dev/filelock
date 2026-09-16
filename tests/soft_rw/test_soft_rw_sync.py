from __future__ import annotations

import multiprocessing as mp
import os
import signal
import stat
import sys
import threading
import time
from contextlib import closing, suppress
from errno import EIO
from multiprocessing import Event, Process
from pathlib import Path
from typing import TYPE_CHECKING, Final, Literal

import pytest
from capabilities import CAPABILITIES

from filelock import AsyncSoftReadWriteLock, SoftFileLockProtocolError, Timeout
from filelock._lease import LeaseCompromise
from filelock._soft_rw import SoftReadWriteLock
from filelock._soft_rw import _storage as storage_mod
from filelock._soft_rw import _sync as sync_mod
from filelock._soft_rw._protocol import GenerationLog, Snapshot, encode_holder, new_token
from filelock._soft_rw._storage import OsFiles
from tests.capability_marks import NEEDS_FILE_MODE, NEEDS_FORK, NEEDS_POSIX_SIGNALS, SKIP_ON_UNRELIABLE_PROCESS_SYNC
from tests.process_helpers import cleanup_processes

if TYPE_CHECKING:
    from collections.abc import Callable, Generator
    from multiprocessing.synchronize import Event as EventType

    from pytest_mock import MockerFixture

pytestmark = pytest.mark.requires_hard_links


_OWNER_READ_WRITE: Final[int] = 0o600
_OWNER_ONLY: Final[int] = 0o700
_FOREIGN_HOST: Final[str] = "terminated-pod"

# Bounds how long a spawned process or thread may take to reach the lock, not how fast it must be: an interpreter
# that starts slowly under a loaded suite is not a locking failure. The short negative waits below are deliberate,
# since those assert a contender stays blocked and have to stay brief.
_PROCESS_DEADLINE: Final[int] = 30


@pytest.fixture(autouse=True)
def _clear_singletons() -> Generator[None]:
    SoftReadWriteLock._instances.clear()
    yield
    for lock in filter(None, (ref() for ref in list(SoftReadWriteLock._instances.valuerefs()))):
        lock.close()
    SoftReadWriteLock._instances.clear()


@pytest.fixture
def lock_file(tmp_path: Path) -> str:
    return str(tmp_path / "test.lock")


def _state(lock_file: str) -> Snapshot:
    return GenerationLog(OsFiles(lock_file), lock_file, f"{lock_file}.rw").latest()


def _holders(lock_file: str) -> list[str]:
    holders = Path(f"{lock_file}.rw", "holders")
    return sorted(entry.name for entry in holders.iterdir()) if holders.is_dir() else []


def _generations(lock_file: str) -> list[str]:
    return sorted(entry.name for entry in Path(f"{lock_file}.rw", "gen").iterdir() if not entry.name.startswith("."))


def _plant_holder(lock_file: str, *, mode: Literal["read", "write"], host: str = _FOREIGN_HOST) -> str:
    # What a process on another host leaves behind when it dies holding the lock: its record and the snapshot naming it.
    files = OsFiles(lock_file)
    root = f"{lock_file}.rw"
    files.prepare(root)
    token = new_token()
    record = encode_holder(token, new_token()).replace(b"host=", f"host={host}?".encode("ascii"), 1)
    files.create(str(Path(root, "holders", token)), record)
    log = GenerationLog(files, lock_file, root)
    latest = log.latest()
    writer, readers = (token, latest.readers) if mode == "write" else (latest.writer, latest.readers | {token})
    assert log.commit(Snapshot(generation=latest.generation + 1, writer=writer, readers=readers))
    return token


@pytest.mark.parametrize(
    "lock_type",
    [pytest.param(SoftReadWriteLock, id="sync"), pytest.param(AsyncSoftReadWriteLock, id="async")],
)
@pytest.mark.parametrize("cached", [pytest.param(False, id="new"), pytest.param(True, id="cached")])
@pytest.mark.parametrize(
    ("settings", "message"),
    [
        pytest.param({name: value}, name, id=f"{name}-{label}")
        for name in ("heartbeat_interval", "stale_threshold", "poll_interval")
        for label, value in (("nan", float("nan")), ("infinity", float("inf")), ("negative-infinity", float("-inf")))
    ]
    + [
        pytest.param({"heartbeat_interval": sys.float_info.max}, "stale_threshold", id="default-overflow"),
    ],
)
def test_rejects_invalid_intervals(
    tmp_path: Path,
    lock_type: type[SoftReadWriteLock | AsyncSoftReadWriteLock],
    cached: bool,
    settings: dict[str, float],
    message: str,
) -> None:
    path: Final = tmp_path / "timing.lock"
    # Keep the weakly cached instance alive through the second construction.
    existing: Final = SoftReadWriteLock(path) if cached else None
    try:
        with pytest.raises(ValueError, match=rf"{message} must .*finite"):
            lock_type(
                path,
                heartbeat_interval=settings.get("heartbeat_interval", 30),
                stale_threshold=settings.get("stale_threshold"),
                poll_interval=settings.get("poll_interval", 0.25),
            )
    finally:
        if existing is not None:
            existing.close()


def test_rejects_non_positive_heartbeat_interval(lock_file: str) -> None:
    with pytest.raises(ValueError, match="heartbeat_interval must be positive"):
        SoftReadWriteLock(lock_file, heartbeat_interval=0, is_singleton=False)


def test_rejects_stale_threshold_not_greater_than_heartbeat(lock_file: str) -> None:
    with pytest.raises(ValueError, match="stale_threshold must exceed"):
        SoftReadWriteLock(lock_file, heartbeat_interval=10, stale_threshold=5, is_singleton=False)


def test_rejects_non_positive_poll_interval(lock_file: str) -> None:
    with pytest.raises(ValueError, match="poll_interval must be positive"):
        SoftReadWriteLock(lock_file, poll_interval=0, is_singleton=False)


def test_public_attributes(lock_file: str) -> None:
    lock = SoftReadWriteLock(
        lock_file,
        timeout=5,
        blocking=False,
        heartbeat_interval=10,
        stale_threshold=45,
        poll_interval=0.5,
        is_singleton=False,
    )
    try:
        assert lock.lock_file == lock_file
        assert lock.timeout == 5
        assert lock.blocking is False
        assert lock.heartbeat_interval == 10
        assert lock.stale_threshold == 45
        assert lock.poll_interval == pytest.approx(0.5)
    finally:
        lock.close()


def test_default_stale_threshold_is_triple_heartbeat(lock_file: str) -> None:
    lock = SoftReadWriteLock(lock_file, heartbeat_interval=12, is_singleton=False)
    try:
        assert lock.stale_threshold == 36
    finally:
        lock.close()


def test_singleton_returns_same_instance(lock_file: str) -> None:
    first = SoftReadWriteLock(lock_file)
    second = SoftReadWriteLock(lock_file)
    try:
        assert first is second
    finally:
        first.close()


def test_non_singleton_returns_distinct_instances(lock_file: str) -> None:
    first = SoftReadWriteLock(lock_file, is_singleton=False)
    second = SoftReadWriteLock(lock_file, is_singleton=False)
    try:
        assert first is not second
    finally:
        first.close()
        second.close()


def test_singleton_mismatch_raises(lock_file: str) -> None:
    first = SoftReadWriteLock(lock_file, timeout=5)
    try:
        with pytest.raises(ValueError, match="cannot be changed"):
            SoftReadWriteLock(lock_file, timeout=10)
    finally:
        first.close()


def test_get_lock_returns_singleton(lock_file: str) -> None:
    first = SoftReadWriteLock.get_lock(lock_file)
    second = SoftReadWriteLock.get_lock(lock_file)
    try:
        assert first is second
    finally:
        first.close()


def test_leaked_acquired_singleton_is_closed_on_teardown(lock_file: str) -> None:
    # A live heartbeat thread keeps the singleton reachable, so the autouse teardown finds and closes it.
    lock = SoftReadWriteLock(lock_file, heartbeat_interval=0.5)
    lock.acquire_write(timeout=2)
    assert _state(lock_file).writer is not None


def test_reentrant_read_holds_and_releases(lock_file: str) -> None:
    lock = _make_lock(lock_file)
    try:
        with lock.read_lock(timeout=2), lock.read_lock(timeout=2):
            pass
        with lock.read_lock(timeout=2):
            pass
    finally:
        lock.close()


def test_reentrant_write_holds_and_releases(lock_file: str) -> None:
    lock = _make_lock(lock_file)
    try:
        with lock.write_lock(timeout=2), lock.write_lock(timeout=2):
            assert _state(lock_file).writer is not None
        assert _state(lock_file).writer is None
    finally:
        lock.close()


def test_upgrade_from_read_to_write_raises(lock_file: str) -> None:
    lock = _make_lock(lock_file)
    try:
        with lock.read_lock(timeout=2), pytest.raises(RuntimeError, match="upgrade not allowed"):
            lock.acquire_write(timeout=1)
    finally:
        lock.close()


def test_downgrade_from_write_to_read_raises(lock_file: str) -> None:
    lock = _make_lock(lock_file)
    try:
        with lock.write_lock(timeout=2), pytest.raises(RuntimeError, match="downgrade not allowed"):
            lock.acquire_read(timeout=1)
    finally:
        lock.close()


def test_write_lock_is_thread_pinned(lock_file: str) -> None:
    lock = _make_lock(lock_file)
    errors: list[BaseException] = []
    lock.acquire_write(timeout=2)

    def other() -> None:
        try:
            lock.acquire_write(timeout=1, blocking=False)
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=other)
    thread.start()
    thread.join()
    lock.release()
    lock.close()
    assert len(errors) == 1
    assert isinstance(errors[0], (RuntimeError, Timeout))


def test_blocking_acquire_without_timeout_waits_for_release(lock_file: str) -> None:
    # Without a deadline the poll loop sleeps a full interval each round rather than clamping to a budget.
    holder = _make_lock(lock_file)
    holder.acquire_write(timeout=2)
    acquired = threading.Event()

    def waiter() -> None:
        contender = _make_lock(lock_file)
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
        assert acquired.wait(timeout=_PROCESS_DEADLINE)
    finally:
        thread.join(timeout=_PROCESS_DEADLINE)
        holder.close()


def test_release_without_hold_raises(lock_file: str) -> None:
    lock = SoftReadWriteLock(lock_file)
    try:
        with pytest.raises(RuntimeError, match="not held"):
            lock.release()
    finally:
        lock.close()


def test_release_force_without_hold_is_noop(lock_file: str) -> None:
    lock = SoftReadWriteLock(lock_file)
    try:
        lock.release(force=True)
    finally:
        lock.close()


def test_release_force_on_reentrant_lock_drops_all(lock_file: str) -> None:
    lock = _make_lock(lock_file)
    try:
        lock.acquire_read(timeout=2)
        lock.acquire_read(timeout=2)
        lock.release(force=True)
        with lock.write_lock(timeout=2):
            pass
    finally:
        lock.close()


def test_close_is_idempotent(lock_file: str) -> None:
    lock = SoftReadWriteLock(lock_file)
    lock.close()
    lock.close()


def test_acquire_on_closed_raises(lock_file: str) -> None:
    lock = SoftReadWriteLock(lock_file)
    lock.close()
    with pytest.raises(RuntimeError, match="has been closed"):
        lock.acquire_read(timeout=1)
    with pytest.raises(RuntimeError, match="has been closed"):
        lock.acquire_write(timeout=1)


@SKIP_ON_UNRELIABLE_PROCESS_SYNC
@pytest.mark.timeout(_PROCESS_DEADLINE * 5)
def test_multiple_readers_can_hold_simultaneously(lock_file: str) -> None:
    r1, r2, release = Event(), Event(), Event()
    p1 = Process(target=_worker, args=(lock_file, "read", r1, release))
    p2 = Process(target=_worker, args=(lock_file, "read", r2, release))
    with cleanup_processes([p1, p2]):
        p1.start()
        p2.start()
        assert r1.wait(timeout=_PROCESS_DEADLINE)
        assert r2.wait(timeout=_PROCESS_DEADLINE)
        release.set()
        p1.join(timeout=_PROCESS_DEADLINE)
        p2.join(timeout=_PROCESS_DEADLINE)


@SKIP_ON_UNRELIABLE_PROCESS_SYNC
@pytest.mark.timeout(_PROCESS_DEADLINE * 4)
def test_write_lock_excludes_writers(lock_file: str) -> None:
    held, release = Event(), Event()
    second = Event()
    holder = Process(target=_worker, args=(lock_file, "write", held, release))
    contender = Process(target=_worker, args=(lock_file, "write", second, None, 0.3, True))
    with cleanup_processes([holder, contender]):
        holder.start()
        assert held.wait(timeout=_PROCESS_DEADLINE)
        contender.start()
        assert not second.wait(timeout=0.5)
        release.set()
        holder.join(timeout=_PROCESS_DEADLINE)
        contender.join(timeout=_PROCESS_DEADLINE)


@SKIP_ON_UNRELIABLE_PROCESS_SYNC
@pytest.mark.timeout(_PROCESS_DEADLINE * 4)
def test_write_lock_excludes_readers(lock_file: str) -> None:
    held, release = Event(), Event()
    reader_acquired = Event()
    writer = Process(target=_worker, args=(lock_file, "write", held, release))
    reader = Process(target=_worker, args=(lock_file, "read", reader_acquired, None, 0.3, True))
    with cleanup_processes([writer, reader]):
        writer.start()
        assert held.wait(timeout=_PROCESS_DEADLINE)
        reader.start()
        assert not reader_acquired.wait(timeout=0.5)
        release.set()
        writer.join(timeout=_PROCESS_DEADLINE)
        reader.join(timeout=_PROCESS_DEADLINE)


@SKIP_ON_UNRELIABLE_PROCESS_SYNC
@pytest.mark.timeout(_PROCESS_DEADLINE * 5)
def test_writer_drains_existing_readers(lock_file: str) -> None:
    r_held, r_release = Event(), Event()
    w_held = Event()
    reader = Process(target=_worker, args=(lock_file, "read", r_held, r_release))
    writer = Process(target=_worker, args=(lock_file, "write", w_held))
    with cleanup_processes([reader, writer]):
        reader.start()
        assert r_held.wait(timeout=_PROCESS_DEADLINE)
        writer.start()
        assert not w_held.wait(timeout=0.5)
        r_release.set()
        reader.join(timeout=_PROCESS_DEADLINE)
        assert w_held.wait(timeout=_PROCESS_DEADLINE)
        writer.join(timeout=_PROCESS_DEADLINE)


@SKIP_ON_UNRELIABLE_PROCESS_SYNC
@pytest.mark.timeout(_PROCESS_DEADLINE * 7)
def test_writer_preference_blocks_new_readers(lock_file: str) -> None:
    r1_held, r1_release = Event(), Event()
    w_held, w_release = Event(), Event()
    r2_held = Event()
    reader1 = Process(target=_worker, args=(lock_file, "read", r1_held, r1_release))
    writer = Process(target=_worker, args=(lock_file, "write", w_held, w_release))
    reader2 = Process(target=_worker, args=(lock_file, "read", r2_held, None, 10, True))
    with cleanup_processes([reader1, writer, reader2]):
        reader1.start()
        assert r1_held.wait(timeout=_PROCESS_DEADLINE)
        writer.start()
        # Start reader2 only once the writer's marker is on disk: a fixed sleep undershoots on a Windows runner still
        # spawning the writer's interpreter, and reader2 then slips in ahead of the writer's intent.
        deadline = time.monotonic() + _PROCESS_DEADLINE
        while _state(lock_file).writer is None:
            assert time.monotonic() < deadline
            time.sleep(0.01)
        reader2.start()
        assert not r2_held.wait(timeout=0.5)
        r1_release.set()
        assert w_held.wait(timeout=_PROCESS_DEADLINE)
        assert not r2_held.wait(timeout=0.3)
        w_release.set()
        assert r2_held.wait(timeout=_PROCESS_DEADLINE)
        reader1.join(timeout=_PROCESS_DEADLINE)
        writer.join(timeout=_PROCESS_DEADLINE)
        reader2.join(timeout=_PROCESS_DEADLINE)


@pytest.mark.timeout(_PROCESS_DEADLINE * 4)
def test_transaction_lock_timeout_across_threads(lock_file: str) -> None:
    # Two threads share one lock instance. Thread A holds the transaction lock while spinning on a peer
    # writer; thread B times out on the transaction lock, exercising the in-process Timeout path rather
    # than cross-process contention.
    # Staleness is not under test: a peer heartbeat that stalls past a short threshold on a loaded runner would let
    # thread A evict the peer and take the lock, turning thread B's Timeout into a same-instance ownership error.
    peer = SoftReadWriteLock(
        lock_file,
        is_singleton=False,
        heartbeat_interval=0.1,
        stale_threshold=_PROCESS_DEADLINE,
        poll_interval=0.02,
    )
    peer.acquire_write(timeout=2)
    try:
        lock = SoftReadWriteLock(
            lock_file,
            is_singleton=False,
            heartbeat_interval=0.1,
            stale_threshold=_PROCESS_DEADLINE,
            poll_interval=0.02,
        )
        try:
            thread_ready = threading.Event()
            release_thread = threading.Event()

            def target_a() -> None:
                thread_ready.set()
                with suppress(Timeout):
                    lock.acquire_write(timeout=2)
                release_thread.wait(timeout=_PROCESS_DEADLINE)

            thread_a = threading.Thread(target=target_a)
            thread_a.start()
            try:
                thread_ready.wait(timeout=_PROCESS_DEADLINE)
                time.sleep(0.05)
                with pytest.raises(Timeout):
                    lock.acquire_write(timeout=0.1)
            finally:
                release_thread.set()
                thread_a.join(timeout=_PROCESS_DEADLINE)
        finally:
            lock.close()
    finally:
        peer.release()
        peer.close()


@pytest.mark.timeout(_PROCESS_DEADLINE * 3)
def test_two_readers_in_same_process_share_slot(lock_file: str) -> None:
    # Many threads take a read lock on one instance; one hits the inner reentrant branch (lock level
    # above 0 after waiting on the transaction lock).
    lock = SoftReadWriteLock(
        lock_file,
        is_singleton=False,
        heartbeat_interval=0.1,
        stale_threshold=0.5,
        poll_interval=0.02,
    )
    try:
        barrier = threading.Barrier(8)

        def target() -> None:
            barrier.wait(timeout=_PROCESS_DEADLINE)
            with lock.read_lock(timeout=5):
                time.sleep(0.05)

        threads = [threading.Thread(target=target) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=_PROCESS_DEADLINE)
    finally:
        lock.close()


@SKIP_ON_UNRELIABLE_PROCESS_SYNC
@pytest.mark.timeout(_PROCESS_DEADLINE * 3)
def test_timeout_raises(lock_file: str) -> None:
    held, release = Event(), Event()
    holder = Process(target=_worker, args=(lock_file, "write", held, release))
    with cleanup_processes([holder]):
        holder.start()
        assert held.wait(timeout=_PROCESS_DEADLINE)
        lock = _make_lock(lock_file)
        try:
            with pytest.raises(Timeout):
                lock.acquire_write(timeout=0.3)
        finally:
            lock.close()
        release.set()
        holder.join(timeout=_PROCESS_DEADLINE)


@SKIP_ON_UNRELIABLE_PROCESS_SYNC
@pytest.mark.timeout(_PROCESS_DEADLINE * 3)
def test_non_blocking_writer_contended_raises(lock_file: str) -> None:
    held, release = Event(), Event()
    holder = Process(target=_worker, args=(lock_file, "write", held, release))
    with cleanup_processes([holder]):
        holder.start()
        assert held.wait(timeout=_PROCESS_DEADLINE)
        lock = _make_lock(lock_file)
        try:
            with pytest.raises(Timeout):
                lock.acquire_write(timeout=1, blocking=False)
            with pytest.raises(Timeout):
                lock.acquire_read(timeout=1, blocking=False)
        finally:
            lock.close()
        release.set()
        holder.join(timeout=_PROCESS_DEADLINE)


@pytest.mark.timeout(10)
def test_writer_phase2_timeout_releases_marker(lock_file: str) -> None:
    # A live reader whose heartbeat stays fresh blocks the phase-2 drain; the writer must abandon its
    # phase-1 claim so the next writer can retry.
    reader = _make_lock(lock_file)
    reader.acquire_read(timeout=2)
    try:
        writer = _make_lock(lock_file)
        try:
            with pytest.raises(Timeout):
                writer.acquire_write(timeout=0.3)
        finally:
            writer.close()
        assert _state(lock_file).writer is None
        assert len(_holders(lock_file)) == 1
    finally:
        reader.release()
        reader.close()


@SKIP_ON_UNRELIABLE_PROCESS_SYNC
@NEEDS_POSIX_SIGNALS
@pytest.mark.timeout(_PROCESS_DEADLINE * 3)
def test_dead_writer_evicted_by_reader(lock_file: str) -> None:  # pragma: needs posix-signals
    held = Event()
    holder = Process(target=_sigkill_worker, args=(lock_file, "write", held, 0.1, 0.5))
    with cleanup_processes([holder]):
        holder.start()
        assert held.wait(timeout=_PROCESS_DEADLINE)
        pid = holder.pid
        assert pid is not None
        os.kill(pid, getattr(signal, "SIGKILL"))  # ruff:ignore[get-attr-with-constant] - signal.SIGKILL is POSIX-only
        holder.join(timeout=_PROCESS_DEADLINE)
        time.sleep(0.8)
        lock = _make_lock(lock_file)
        try:
            with lock.read_lock(timeout=5):
                pass
        finally:
            lock.close()
        assert _state(lock_file).writer is None


@SKIP_ON_UNRELIABLE_PROCESS_SYNC
@NEEDS_POSIX_SIGNALS
@pytest.mark.timeout(_PROCESS_DEADLINE * 3)
def test_dead_reader_evicted_by_writer(lock_file: str) -> None:  # pragma: needs posix-signals
    held = Event()
    holder = Process(target=_sigkill_worker, args=(lock_file, "read", held, 0.1, 0.5))
    with cleanup_processes([holder]):
        holder.start()
        assert held.wait(timeout=_PROCESS_DEADLINE)
        pid = holder.pid
        assert pid is not None
        os.kill(pid, getattr(signal, "SIGKILL"))  # ruff:ignore[get-attr-with-constant] - signal.SIGKILL is POSIX-only
        holder.join(timeout=_PROCESS_DEADLINE)
        time.sleep(0.8)
        lock = _make_lock(lock_file)
        try:
            with lock.write_lock(timeout=5):
                pass
        finally:
            lock.close()


def test_heartbeat_reports_eviction_and_stops(lock_file: str) -> None:
    # A peer that waited out the stale threshold commits a snapshot without this holder. The next heartbeat sees the
    # snapshot no longer naming it, records the loss, and stops refreshing a claim that no longer exists.
    seen: list[LeaseCompromise] = []
    lock = _make_lock(lock_file, heartbeat_interval=0.05, stale_threshold=0.2, on_compromise=seen.append)
    lock.acquire_write(timeout=2)
    try:
        hold = lock._hold
        assert hold is not None
        log = GenerationLog(OsFiles(lock_file), lock_file, f"{lock_file}.rw")
        latest = log.latest()
        assert log.commit(Snapshot(generation=latest.generation + 1, writer=None, readers=frozenset()))
        assert hold.heartbeat_stop.wait(timeout=_PROCESS_DEADLINE)
        assert lock.compromise == LeaseCompromise(lock_file=lock_file, token=hold.participant.token, reason="evicted")
        assert seen == [lock.compromise]
    finally:
        lock.release(force=True)
        lock.close()
    peer = _make_lock(lock_file, heartbeat_interval=0.05, stale_threshold=0.2)
    try:
        with peer.write_lock(timeout=1):
            pass
    finally:
        peer.close()


def test_heartbeat_reports_a_removed_holder_record(lock_file: str) -> None:
    lock = _make_lock(lock_file, heartbeat_interval=0.05, stale_threshold=0.2)
    lock.acquire_read(timeout=2)
    try:
        hold = lock._hold
        assert hold is not None
        Path(f"{lock_file}.rw", "holders", hold.participant.token).unlink()
        assert hold.heartbeat_stop.wait(timeout=_PROCESS_DEADLINE)
        assert lock.compromise is not None
        assert lock.compromise.reason == "evicted"
    finally:
        lock.release(force=True)
        lock.close()


def test_release_leaves_a_peers_claim_alone(lock_file: str) -> None:
    # A holder paused past the stale threshold (GC pause, SIGSTOP, suspended VM) can be evicted, after which a peer
    # holds the writer slot. Releasing must only ever commit this holder out, never touch the peer's claim.
    lock = _make_lock(lock_file, heartbeat_interval=10, stale_threshold=40)
    lock.acquire_write(timeout=2)
    try:
        peer = _plant_holder(lock_file, mode="write")
        lock.release()
        assert _state(lock_file).writer == peer
        assert _holders(lock_file) == [peer]
    finally:
        lock.close()


def test_writer_evicted_while_draining_queues_behind_its_evictor(
    lock_file: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A writer paused past stale_threshold while waiting for readers can be evicted; a peer then names itself writer.
    # The resumed writer must not finish its acquire as if it still held the slot. Instead it queues again.
    reader = _make_lock(lock_file, heartbeat_interval=10, stale_threshold=40)
    reader.acquire_read(timeout=2)
    writer = _make_lock(lock_file, heartbeat_interval=10, stale_threshold=40)
    real_sleep = time.sleep
    swapped = threading.Event()
    peer: list[str] = []

    def hook(seconds: float) -> None:  # ruff:ignore[unused-function-argument]  # replaces time.sleep; the duration is irrelevant to the swap
        if swapped.is_set():
            raise Timeout(lock_file)
        swapped.set()
        # The peer evicts the draining writer and takes the slot; the reader leaves so only the slot blocks the writer.
        files = OsFiles(lock_file)
        log = GenerationLog(files, lock_file, f"{lock_file}.rw")
        latest = log.latest()
        peer.append(_plant_holder(lock_file, mode="read"))
        latest = log.latest()
        assert log.commit(Snapshot(generation=latest.generation + 1, writer=peer[0], readers=frozenset()))
        reader.release()

    monkeypatch.setattr(sync_mod.time, "sleep", hook)
    try:
        with pytest.raises(Timeout):
            writer.acquire_write(timeout=30)
        assert swapped.is_set()
        assert _state(lock_file).writer == peer[0]
    finally:
        monkeypatch.setattr(sync_mod.time, "sleep", real_sleep)
        writer.close()
        reader.close()


def test_heartbeat_survives_a_transient_refresh_error(lock_file: str, mocker: MockerFixture) -> None:
    # On the NFS-style filesystems this lock targets, a transient ESTALE/EIO on the heartbeat is routine; it must not
    # kill the heartbeat and drop the claim while we still believe we hold it.
    lock = _make_lock(lock_file, heartbeat_interval=0.02, stale_threshold=0.5)
    lock.acquire_write(timeout=2)
    try:
        hold = lock._hold
        assert hold is not None
        mocker.patch.object(storage_mod.OsFiles, "overwrite", side_effect=OSError(EIO, "Input/output error"))
        time.sleep(0.2)  # ~10 ticks, every one failing the refresh
        assert hold.heartbeat_thread.is_alive()
        assert not hold.heartbeat_stop.is_set()
        assert lock.compromise is None
    finally:
        mocker.stopall()
        lock.release(force=True)
        lock.close()


def test_heartbeat_reports_refresh_failures_that_outlast_the_margin(lock_file: str, mocker: MockerFixture) -> None:
    # Failures that run long enough for a peer to evict the record before the next success could land are a loss the
    # holder must hear about, a margin before the record actually ages out.
    seen: list[LeaseCompromise] = []
    lock = _make_lock(lock_file, heartbeat_interval=0.02, stale_threshold=0.1, on_compromise=seen.append)
    lock.acquire_write(timeout=2)
    try:
        hold = lock._hold
        assert hold is not None
        mocker.patch.object(storage_mod.OsFiles, "overwrite", side_effect=OSError(EIO, "Input/output error"))
        assert hold.heartbeat_stop.wait(timeout=_PROCESS_DEADLINE)
        assert [compromise.reason for compromise in seen] == ["refresh-failed"]
    finally:
        mocker.stopall()
        lock.release(force=True)
        lock.close()


@pytest.mark.parametrize("mode", [pytest.param("write", id="write"), pytest.param("read", id="read")])
def test_acquire_hands_back_the_slot_when_the_heartbeat_cannot_start(
    lock_file: str, mocker: MockerFixture, mode: Literal["read", "write"]
) -> None:
    # A heartbeat thread the OS refuses (an rlimit reached) must not leave a hold behind: a peer would evict the
    # unrefreshed record and acquire while this instance still believed it held the lock, and release() would raise
    # joining a thread that never started.
    mocker.patch.object(sync_mod._HeartbeatThread, "start", side_effect=RuntimeError("can't start new thread"))
    lock = _make_lock(lock_file)
    acquire = lock.acquire_write if mode == "write" else lock.acquire_read
    with pytest.raises(RuntimeError, match="can't start new thread"):
        acquire(timeout=2)

    assert lock._hold is None
    assert _state(lock_file).members == frozenset()
    assert _holders(lock_file) == []
    lock.release(force=True)  # a handed-back slot leaves nothing to release, so this must not raise
    lock.close()


def test_short_attempts_still_evict_a_dead_holder(lock_file: str) -> None:
    # How long a peer's record has stayed unchanged is knowledge the lock keeps across attempts: a caller whose every
    # attempt is shorter than the stale threshold (a retry loop, blocking=False) must still get past a crashed holder.
    _plant_holder(lock_file, mode="write")
    lock = _make_lock(lock_file, heartbeat_interval=0.1, stale_threshold=0.3)
    try:

        def attempt() -> bool:
            try:
                lock.acquire_read(blocking=False)
            except Timeout:
                return False
            return True

        deadline = time.monotonic() + 5
        while not attempt():
            assert time.monotonic() < deadline
            time.sleep(0.02)
        lock.release()
    finally:
        lock.close()


def test_singleton_rejects_a_different_on_compromise(lock_file: str) -> None:
    seen: list[LeaseCompromise] = []
    first = SoftReadWriteLock(lock_file, on_compromise=seen.append)
    try:
        assert SoftReadWriteLock(lock_file) is first
        assert SoftReadWriteLock(lock_file, on_compromise=seen.append) is first
        with pytest.raises(ValueError, match="different on_compromise"):
            SoftReadWriteLock(lock_file, on_compromise=seen.remove)
    finally:
        first.close()


def test_release_from_on_compromise_leaves_cleanly(lock_file: str) -> None:
    # The callback runs on the heartbeat thread; a release there has no thread to join and must still leave.
    lock = _make_lock(lock_file, heartbeat_interval=0.05, stale_threshold=0.2)
    lock = SoftReadWriteLock(
        lock_file,
        is_singleton=False,
        heartbeat_interval=0.05,
        stale_threshold=0.2,
        poll_interval=0.02,
        on_compromise=lambda _compromise: lock.release(force=True),
    )
    lock.acquire_write(timeout=2)
    hold = lock._hold
    assert hold is not None
    try:
        log = GenerationLog(OsFiles(lock_file), lock_file, f"{lock_file}.rw")
        latest = log.latest()
        assert log.commit(Snapshot(generation=latest.generation + 1, writer=None, readers=frozenset()))
        assert hold.heartbeat_stop.wait(timeout=_PROCESS_DEADLINE)
        hold.heartbeat_thread.join(timeout=_PROCESS_DEADLINE)
        assert lock._hold is None
        assert _holders(lock_file) == []
    finally:
        lock.close()


def test_lock_file_in_a_missing_directory_is_created(tmp_path: Path) -> None:
    lock = _make_lock(str(tmp_path / "nested" / "deeper" / "x.lock"))
    try:
        with lock.write_lock(timeout=1):
            pass
    finally:
        lock.close()


def test_rejects_poll_interval_not_below_stale_threshold(lock_file: str) -> None:
    with pytest.raises(ValueError, match="poll_interval must be below"):
        SoftReadWriteLock(lock_file, heartbeat_interval=1, stale_threshold=3, poll_interval=3, is_singleton=False)


def test_late_heartbeat_tick_does_not_stamp_a_later_hold(lock_file: str) -> None:
    # A tick that outlived its release's join reports on the hold it served, never on whatever was acquired since.
    lock = _make_lock(lock_file, heartbeat_interval=10, stale_threshold=40)
    lock.acquire_write(timeout=2)
    old = lock._hold
    assert old is not None
    lock.release()
    lock.acquire_write(timeout=2)
    try:
        lock._report_compromise(old, "evicted", None)
        assert lock.compromise is None
    finally:
        lock.release()
        lock.close()


@SKIP_ON_UNRELIABLE_PROCESS_SYNC
@pytest.mark.timeout(_PROCESS_DEADLINE * 3)
def test_live_heartbeat_keeps_lock_alive_past_stale_threshold(lock_file: str) -> None:
    # Generous timing here so the test stays stable on slow Windows runners where the holder's
    # multiprocessing.spawn startup, the heartbeat thread scheduling, and the parent's clock resolution
    # can all introduce sub-second jitter.
    heartbeat, stale = 0.3, 1.5
    held, release = Event(), Event()
    holder = Process(
        target=_worker,
        args=(lock_file, "write", held, release, -1, True, heartbeat, stale, 0.05),
    )
    with cleanup_processes([holder]):
        holder.start()
        assert held.wait(timeout=_PROCESS_DEADLINE)
        time.sleep(stale * 2)
        lock = _make_lock(lock_file, heartbeat_interval=heartbeat, stale_threshold=stale)
        try:
            with pytest.raises(Timeout):
                lock.acquire_write(timeout=stale * 2)
        finally:
            lock.close()
        release.set()
        holder.join(timeout=_PROCESS_DEADLINE)


def test_generation_is_a_fencing_token(lock_file: str) -> None:
    first = _make_lock(lock_file)
    second = _make_lock(lock_file)
    try:
        assert first.generation is None
        with first.write_lock(timeout=2):
            granted = first.generation
            assert granted is not None
        with second.write_lock(timeout=2):
            later = second.generation
            assert later is not None
            assert later > granted
        assert second.generation is None
    finally:
        first.close()
        second.close()


def test_foreign_host_holder_is_evicted_after_the_stale_threshold(lock_file: str) -> None:
    # The report behind #725: a pod dies on another host holding the lock, and a replacement pod under a different
    # hostname must still get in. Liveness is a nonce that stopped changing, so the hostname never enters into it.
    _plant_holder(lock_file, mode="write")
    lock = _make_lock(lock_file, heartbeat_interval=0.1, stale_threshold=0.3)
    try:
        started = time.monotonic()
        with pytest.raises(Timeout):
            lock.acquire_read(timeout=0.1)
        # The first attempt already started watching the record, so the wait spans both attempts.
        with lock.read_lock(timeout=5):
            assert time.monotonic() - started >= 0.3 - 0.05
        assert _holders(lock_file) == []
    finally:
        lock.close()


def test_release_never_blocks_on_an_abandoned_claim(lock_file: str) -> None:
    # The other half of #725: a writer holding the lock could not let go while a foreign host's marker sat in the way.
    # Leaving is one commit of the holder's own token, so nothing a dead peer left can block it.
    lock = _make_lock(lock_file, heartbeat_interval=10, stale_threshold=40)
    lock.acquire_write(timeout=2)
    _plant_holder(lock_file, mode="read")
    started = time.monotonic()
    lock.release()
    assert time.monotonic() - started < 1
    assert _state(lock_file).writer is None
    lock.close()


def test_malformed_generation_record_fails_closed(lock_file: str) -> None:
    lock = _make_lock(lock_file)
    try:
        with lock.write_lock(timeout=2):
            pass
        latest = _generations(lock_file)[-1]
        Path(f"{lock_file}.rw", "gen", f"{int(latest) + 1:020d}").write_bytes(b"garbage\n")
        with pytest.raises(SoftFileLockProtocolError, match="malformed generation record"):
            lock.acquire_write(timeout=1)
    finally:
        lock.close()


def test_malformed_holder_record_is_evicted(lock_file: str) -> None:
    # A record that cannot be parsed still identifies nobody who refreshes it, so it ages out like any other.
    token = _plant_holder(lock_file, mode="write")
    Path(f"{lock_file}.rw", "holders", token).write_bytes(b"\x00garbage")
    lock = _make_lock(lock_file, heartbeat_interval=0.1, stale_threshold=0.3)
    try:
        with lock.write_lock(timeout=5):
            pass
    finally:
        lock.close()


def test_temporary_commit_files_are_swept(lock_file: str) -> None:
    orphan = Path(f"{lock_file}.rw", "gen", ".commit-abandoned")
    lock = _make_lock(lock_file, heartbeat_interval=0.05, stale_threshold=0.2)
    try:
        with lock.write_lock(timeout=2):
            pass
        orphan.write_bytes(b"left by a crash between create and link")
        with lock.write_lock(timeout=2):
            time.sleep(0.6)
        assert not orphan.exists()
    finally:
        lock.close()


def test_generations_are_compacted(lock_file: str) -> None:
    lock = _make_lock(lock_file)
    try:
        for _ in range(80):
            with lock.write_lock(timeout=2):
                pass
        assert len(_generations(lock_file)) <= 66
    finally:
        lock.close()


def test_a_participant_behind_a_compacted_generation_rescans(lock_file: str) -> None:
    lock = _make_lock(lock_file)
    peer = _make_lock(lock_file)
    try:
        with lock.write_lock(timeout=2):
            pass
        stale_log = GenerationLog(OsFiles(lock_file), lock_file, f"{lock_file}.rw")
        remembered = stale_log.latest()
        for _ in range(40):
            with peer.write_lock(timeout=2):
                pass
        assert not Path(f"{lock_file}.rw", "gen", f"{remembered.generation:020d}").exists()
        assert stale_log.latest().generation == _state(lock_file).generation
    finally:
        lock.close()
        peer.close()


@pytest.mark.skipif(not CAPABILITIES["symlink"], reason="staging the protocol directory as a symlink")
def test_symlinked_protocol_directory_is_refused(lock_file: str, tmp_path: Path) -> None:  # pragma: needs symlink
    victim_dir = tmp_path / "victim_dir"
    victim_dir.mkdir()
    Path(f"{lock_file}.rw").symlink_to(victim_dir)
    lock = _make_lock(lock_file)
    try:
        with pytest.raises(RuntimeError, match="not a directory or is a symlink"):
            lock.acquire_read(timeout=0.5)
    finally:
        lock.close()
    assert list(victim_dir.iterdir()) == []


def test_protocol_path_as_regular_file_is_refused(lock_file: str) -> None:
    Path(f"{lock_file}.rw").write_bytes(b"x")
    lock = _make_lock(lock_file)
    try:
        with pytest.raises(RuntimeError, match="not a directory or is a symlink"):
            lock.acquire_read(timeout=0.5)
    finally:
        lock.close()


@NEEDS_FILE_MODE
def test_records_are_owner_only(lock_file: str) -> None:  # pragma: needs file-mode
    lock = _make_lock(lock_file)
    try:
        with lock.write_lock(timeout=2):
            root = Path(f"{lock_file}.rw")
            for directory in (root, root / "gen", root / "holders"):
                assert stat.S_IMODE(directory.lstat().st_mode) == _OWNER_ONLY
            generation = root / "gen" / _generations(lock_file)[-1]
            assert stat.S_IMODE(generation.lstat().st_mode) == _OWNER_READ_WRITE
            (holder,) = _holders(lock_file)
            assert stat.S_IMODE((root / "holders" / holder).lstat().st_mode) == _OWNER_READ_WRITE
    finally:
        lock.close()


def test_stray_files_in_the_protocol_directories_are_ignored(lock_file: str) -> None:
    root = Path(f"{lock_file}.rw")
    (root / "gen").mkdir(parents=True)
    (root / "holders").mkdir()
    (root / "gen" / ".hidden").write_bytes(b"ignored")
    (root / "gen" / "not-a-generation").write_bytes(b"ignored")
    (root / "holders" / "not-a-token").write_bytes(b"ignored")
    lock = _make_lock(lock_file)
    try:
        with lock.write_lock(timeout=2):
            pass
    finally:
        lock.close()


@SKIP_ON_UNRELIABLE_PROCESS_SYNC
@NEEDS_FORK
@pytest.mark.timeout(_PROCESS_DEADLINE * 2)
def test_child_cannot_reuse_parents_lock_instance(tmp_path: Path) -> None:  # pragma: needs fork
    ctx = mp.get_context("spawn")
    result, failure = ctx.Event(), ctx.Event()
    proc = ctx.Process(target=_reuse_inherited_lock, args=(str(tmp_path / "foo.lock"), result, failure))
    with cleanup_processes([proc]):
        proc.start()
        proc.join(timeout=_PROCESS_DEADLINE)
        assert not failure.is_set()
        assert result.is_set()


@SKIP_ON_UNRELIABLE_PROCESS_SYNC
@NEEDS_FORK
@pytest.mark.timeout(_PROCESS_DEADLINE * 2)
def test_child_release_on_inherited_lock_is_silent(tmp_path: Path) -> None:  # pragma: needs fork
    ctx = mp.get_context("spawn")
    result, failure = ctx.Event(), ctx.Event()
    proc = ctx.Process(target=_release_inherited_lock, args=(str(tmp_path / "foo.lock"), result, failure))
    with cleanup_processes([proc]):
        proc.start()
        proc.join(timeout=_PROCESS_DEADLINE)
        assert not failure.is_set()
        assert result.is_set()


@SKIP_ON_UNRELIABLE_PROCESS_SYNC
@NEEDS_FORK
@pytest.mark.timeout(_PROCESS_DEADLINE * 2)
def test_child_can_acquire_a_different_lock_after_fork(tmp_path: Path) -> None:  # pragma: needs fork
    ctx = mp.get_context("spawn")
    result, failure = ctx.Event(), ctx.Event()
    proc = ctx.Process(
        target=_reacquire_fresh_lock_in_child,
        args=(str(tmp_path / "parent.lock"), str(tmp_path / "child.lock"), result, failure),
    )
    with cleanup_processes([proc]):
        proc.start()
        proc.join(timeout=_PROCESS_DEADLINE)
        assert not failure.is_set()
        assert result.is_set()


@NEEDS_FORK
@pytest.mark.timeout(_PROCESS_DEADLINE * 2)
# Holding the write lock keeps the heartbeat thread alive, so this fork is necessarily from a multi-threaded
# process and Python 3.15 warns that it may deadlock. That is the scenario under test, and it is already safe:
# register_at_fork resets inherited state in the child (any child use raises "invalidated by fork()"). Expected.
@SKIP_ON_UNRELIABLE_PROCESS_SYNC
@pytest.mark.filterwarnings("ignore:.*multi-threaded, use of fork.*:DeprecationWarning")
def test_parent_retains_lock_across_fork(tmp_path: Path) -> None:  # pragma: needs fork
    path = str(tmp_path / "foo.lock")
    lock = SoftReadWriteLock(path, heartbeat_interval=0.2, stale_threshold=1.0, poll_interval=0.02)
    lock.acquire_write(timeout=5)
    try:
        child = _fork_process(target=time.sleep, args=(0.05,))
        child.start()
        child.join(timeout=_PROCESS_DEADLINE)
        assert _state(path).writer is not None
        peer = SoftReadWriteLock(
            path,
            heartbeat_interval=0.2,
            stale_threshold=1.0,
            poll_interval=0.02,
            is_singleton=False,
        )
        try:
            with pytest.raises(Timeout):
                peer.acquire_write(timeout=0.3)
        finally:
            peer.close()
    finally:
        lock.release()
        lock.close()
    assert _state(path).writer is None


def _make_lock(
    path: str,
    *,
    heartbeat_interval: float = 0.1,
    stale_threshold: float = 0.5,
    poll_interval: float = 0.02,
    is_singleton: bool = False,
    on_compromise: Callable[[LeaseCompromise], None] | None = None,
) -> SoftReadWriteLock:
    return SoftReadWriteLock(
        path,
        heartbeat_interval=heartbeat_interval,
        stale_threshold=stale_threshold,
        poll_interval=poll_interval,
        is_singleton=is_singleton,
        on_compromise=on_compromise,
    )


def _worker(
    lock_file: str,
    mode: Literal["read", "write"],
    acquired_event: EventType,
    release_event: EventType | None = None,
    timeout: float = -1,
    blocking: bool = True,
    heartbeat_interval: float = 0.1,
    stale_threshold: float = 1.0,
    poll_interval: float = 0.02,
) -> None:
    lock = SoftReadWriteLock(
        lock_file,
        timeout=timeout,
        blocking=blocking,
        is_singleton=False,
        heartbeat_interval=heartbeat_interval,
        stale_threshold=stale_threshold,
        poll_interval=poll_interval,
    )
    try:
        with lock.read_lock() if mode == "read" else lock.write_lock():
            acquired_event.set()
            if release_event is not None:
                release_event.wait(timeout=_PROCESS_DEADLINE)
            else:
                time.sleep(0.2)
    finally:
        lock.close()


def _sigkill_worker(  # pragma: forked child
    lock_file: str,
    mode: Literal["read", "write"],
    acquired_event: EventType,
    heartbeat_interval: float,
    stale_threshold: float,
) -> None:
    lock = SoftReadWriteLock(
        lock_file,
        is_singleton=False,
        heartbeat_interval=heartbeat_interval,
        stale_threshold=stale_threshold,
        poll_interval=0.05,
    )
    if mode == "read":
        lock.acquire_read()
    else:
        lock.acquire_write()
    acquired_event.set()
    time.sleep(60)


def _reuse_inherited_lock(lock_file: str, result: EventType, failure: EventType) -> None:  # pragma: needs fork
    lock = SoftReadWriteLock(lock_file, heartbeat_interval=0.2, stale_threshold=1.0, poll_interval=0.02)
    lock.acquire_write(timeout=5)
    ok = _fork_event()

    def child_entry() -> None:
        try:
            lock.acquire_read(timeout=1)
        except RuntimeError as exc:
            if "invalidated by fork" in str(exc):  # pragma: no branch  # the inherited lock raises nothing else
                ok.set()

    child = _fork_process(target=child_entry)
    child.start()
    child.join(timeout=_PROCESS_DEADLINE)
    if ok.is_set():
        result.set()
    else:  # pragma: no cover  # reached only if the child fails to report the inherited lock, i.e. a regression
        failure.set()
    lock.release()
    lock.close()


def _release_inherited_lock(lock_file: str, result: EventType, failure: EventType) -> None:  # pragma: needs fork
    lock = SoftReadWriteLock(lock_file, heartbeat_interval=0.2, stale_threshold=1.0, poll_interval=0.02)
    lock.acquire_read(timeout=5)
    ok = _fork_event()

    def child_entry() -> None:
        try:
            lock.release()
        except RuntimeError:  # pragma: no cover  # release on a fork-inherited lock is silent; guards a regression
            return
        ok.set()

    child = _fork_process(target=child_entry)
    child.start()
    child.join(timeout=_PROCESS_DEADLINE)
    if ok.is_set():
        result.set()
    else:  # pragma: no cover  # reached only if the child's silent release regresses into raising
        failure.set()
    lock.release()
    lock.close()


def _reacquire_fresh_lock_in_child(  # pragma: needs fork
    lock_file: str, child_path: str, result: EventType, failure: EventType
) -> None:
    parent_lock = SoftReadWriteLock(lock_file, heartbeat_interval=0.2, stale_threshold=1.0, poll_interval=0.02)
    parent_lock.acquire_write(timeout=5)
    ok = _fork_event()

    def child_entry() -> None:
        child_lock = SoftReadWriteLock(
            child_path,
            is_singleton=False,
            heartbeat_interval=0.2,
            stale_threshold=1.0,
            poll_interval=0.02,
        )
        try:
            with child_lock.read_lock(timeout=2):
                ok.set()
        finally:
            child_lock.close()

    child = _fork_process(target=child_entry)
    child.start()
    child.join(timeout=_PROCESS_DEADLINE)
    if ok.is_set():
        result.set()
    else:  # pragma: no cover  # reached only if the child cannot acquire a fresh lock after the fork, i.e. a regression
        failure.set()
    parent_lock.release()
    parent_lock.close()


def _fork_process(  # pragma: needs fork
    target: Callable[..., object], args: tuple[object, ...] = ()
) -> mp.process.BaseProcess:
    if sys.platform == "win32":  # pragma: win32 cover
        msg = "fork context is POSIX only"
        raise RuntimeError(msg)
    return mp.get_context("fork").Process(target=target, args=args)


def _fork_event() -> EventType:  # pragma: needs fork
    if sys.platform == "win32":  # pragma: win32 cover
        msg = "fork context is POSIX only"
        raise RuntimeError(msg)
    return mp.get_context("fork").Event()


@SKIP_ON_UNRELIABLE_PROCESS_SYNC
def test_cleanup_terminates_a_still_running_process() -> None:
    proc = Process(target=time.sleep, args=(30,))
    proc.start()
    started = time.monotonic()

    with cleanup_processes([proc]):
        assert proc.is_alive()

    # A helper that only joined would sit here for the child's whole sleep instead of signaling it first.
    assert time.monotonic() - started < 10


@SKIP_ON_UNRELIABLE_PROCESS_SYNC
def test_cleanup_closes_the_process() -> None:
    proc = Process(target=time.sleep, args=(30,))
    proc.start()

    with cleanup_processes([proc]):
        assert proc.is_alive()

    with pytest.raises(ValueError, match="process object is closed"):
        proc.is_alive()


def test_record_zero_write_rolls_back(lock_file: str, mocker: MockerFixture) -> None:
    mocker.patch("filelock._util.os.write", return_value=0)

    lock = SoftReadWriteLock(lock_file, is_singleton=False)
    with pytest.raises(OSError, match="0 bytes"):
        lock.acquire_write(timeout=1)
    assert _holders(lock_file) == []


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
    _plant_holder(lock_file, mode="write")
    real_sleep: Final = time.sleep

    def sleep(seconds: float) -> None:
        assert blocking
        assert 0 < seconds <= timeout
        real_sleep(seconds)

    mocker.patch("time.sleep", autospec=True, side_effect=sleep)
    with closing(SoftReadWriteLock(lock_file, timeout=timeout, blocking=blocking, poll_interval=10)) as lock:
        acquire: Final = lock.acquire_read if mode == "read" else lock.acquire_write
        with pytest.raises(Timeout) as caught:
            acquire()
        assert caught.value.lock_file == lock_file
    assert _state(lock_file).readers == frozenset()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", [pytest.param("read", id="read"), pytest.param("write", id="write")])
async def test_async_contention_reports_public_path(lock_file: str, mode: Literal["read", "write"]) -> None:
    _plant_holder(lock_file, mode="write")
    lock: Final = AsyncSoftReadWriteLock(lock_file, timeout=0.02)
    try:
        acquire: Final = lock.acquire_read if mode == "read" else lock.acquire_write
        with pytest.raises(Timeout) as caught:
            await acquire()
        assert caught.value.lock_file == lock_file
    finally:
        await lock.close()


def test_writer_timeout_commits_itself_out(lock_file: str) -> None:
    # A writer that gives up while draining must not stay named: it would block every reader until a peer waited out
    # the stale threshold. Its own token is all it removes, so the reader it waited on keeps its hold.
    reader = _make_lock(lock_file, heartbeat_interval=10, stale_threshold=40)
    writer = _make_lock(lock_file, heartbeat_interval=10, stale_threshold=40, poll_interval=0.01)
    try:
        reader.acquire_read()
        with pytest.raises(Timeout):
            writer.acquire_write(timeout=0.05)
        state = _state(lock_file)
        assert state.writer is None
        assert len(state.readers) == 1
    finally:
        reader.close()
        writer.close()
