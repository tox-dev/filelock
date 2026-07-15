from __future__ import annotations

import multiprocessing as mp
import os
import stat
import sys
import threading
import time
from contextlib import contextmanager, suppress
from errno import EIO, ENOENT
from multiprocessing import Event, Process
from pathlib import Path
from typing import TYPE_CHECKING, Final, Literal

import pytest

from filelock import Timeout
from filelock import _util as util_mod
from filelock._soft_rw import SoftReadWriteLock
from filelock._soft_rw import _sync as sync_mod

if TYPE_CHECKING:
    from collections.abc import Callable, Generator
    from multiprocessing.synchronize import Event as EventType

    from pytest_mock import MockerFixture


_REQUIRES_POSIX_SIGNALS: Final[pytest.MarkDecorator] = pytest.mark.skipif(
    sys.platform == "win32", reason="POSIX signals required"
)
_REQUIRES_POSIX_PERMISSIONS: Final[pytest.MarkDecorator] = pytest.mark.skipif(
    sys.platform == "win32", reason="POSIX file-mode bits not meaningful on Windows"
)
_REQUIRES_FORK: Final[pytest.MarkDecorator] = pytest.mark.skipif(not hasattr(os, "fork"), reason="os.fork required")


@pytest.fixture(autouse=True)
def _clear_singletons() -> Generator[None]:
    SoftReadWriteLock._instances.clear()
    yield
    for ref in list(SoftReadWriteLock._instances.valuerefs()):
        if (lock := ref()) is not None:
            lock.close()
    SoftReadWriteLock._instances.clear()


@pytest.fixture
def lock_file(tmp_path: Path) -> str:
    return str(tmp_path / "test.lock")


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
            assert Path(f"{lock_file}.write").exists()
        assert not Path(f"{lock_file}.write").exists()
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


@pytest.mark.timeout(15)
def test_multiple_readers_can_hold_simultaneously(lock_file: str) -> None:
    r1, r2, release = Event(), Event(), Event()
    p1 = Process(target=_worker, args=(lock_file, "read", r1, release))
    p2 = Process(target=_worker, args=(lock_file, "read", r2, release))
    with _cleanup([p1, p2]):
        p1.start()
        p2.start()
        assert r1.wait(timeout=5)
        assert r2.wait(timeout=5)
        release.set()
        p1.join(timeout=5)
        p2.join(timeout=5)


@pytest.mark.timeout(15)
def test_write_lock_excludes_writers(lock_file: str) -> None:
    held, release = Event(), Event()
    second = Event()
    holder = Process(target=_worker, args=(lock_file, "write", held, release))
    contender = Process(target=_worker, args=(lock_file, "write", second, None, 0.3, True))
    with _cleanup([holder, contender]):
        holder.start()
        assert held.wait(timeout=5)
        contender.start()
        assert not second.wait(timeout=0.5)
        release.set()
        holder.join(timeout=5)
        contender.join(timeout=5)


@pytest.mark.timeout(15)
def test_write_lock_excludes_readers(lock_file: str) -> None:
    held, release = Event(), Event()
    reader_acquired = Event()
    writer = Process(target=_worker, args=(lock_file, "write", held, release))
    reader = Process(target=_worker, args=(lock_file, "read", reader_acquired, None, 0.3, True))
    with _cleanup([writer, reader]):
        writer.start()
        assert held.wait(timeout=5)
        reader.start()
        assert not reader_acquired.wait(timeout=0.5)
        release.set()
        writer.join(timeout=5)
        reader.join(timeout=5)


@pytest.mark.timeout(20)
def test_writer_drains_existing_readers(lock_file: str) -> None:
    r_held, r_release = Event(), Event()
    w_held = Event()
    reader = Process(target=_worker, args=(lock_file, "read", r_held, r_release))
    writer = Process(target=_worker, args=(lock_file, "write", w_held))
    with _cleanup([reader, writer]):
        reader.start()
        assert r_held.wait(timeout=5)
        writer.start()
        assert not w_held.wait(timeout=0.5)
        r_release.set()
        reader.join(timeout=5)
        assert w_held.wait(timeout=5)
        writer.join(timeout=5)


@pytest.mark.timeout(20)
def test_writer_preference_blocks_new_readers(lock_file: str) -> None:
    r1_held, r1_release = Event(), Event()
    w_held, w_release = Event(), Event()
    r2_held = Event()
    reader1 = Process(target=_worker, args=(lock_file, "read", r1_held, r1_release))
    writer = Process(target=_worker, args=(lock_file, "write", w_held, w_release))
    reader2 = Process(target=_worker, args=(lock_file, "read", r2_held, None, 10, True))
    with _cleanup([reader1, writer, reader2]):
        reader1.start()
        assert r1_held.wait(timeout=5)
        writer.start()
        time.sleep(0.3)
        reader2.start()
        assert not r2_held.wait(timeout=0.5)
        r1_release.set()
        assert w_held.wait(timeout=5)
        assert not r2_held.wait(timeout=0.3)
        w_release.set()
        assert r2_held.wait(timeout=5)
        reader1.join(timeout=5)
        writer.join(timeout=5)
        reader2.join(timeout=5)


@pytest.mark.timeout(10)
def test_transaction_lock_timeout_across_threads(lock_file: str) -> None:
    # Two threads share one lock instance. Thread A holds the transaction lock while spinning on a peer
    # writer; thread B times out on the transaction lock, exercising the in-process Timeout path rather
    # than cross-process contention.
    peer = SoftReadWriteLock(
        lock_file,
        is_singleton=False,
        heartbeat_interval=0.1,
        stale_threshold=0.5,
        poll_interval=0.02,
    )
    peer.acquire_write(timeout=2)
    try:
        lock = SoftReadWriteLock(
            lock_file,
            is_singleton=False,
            heartbeat_interval=0.1,
            stale_threshold=0.5,
            poll_interval=0.02,
        )
        try:
            thread_ready = threading.Event()
            release_thread = threading.Event()

            def target_a() -> None:
                thread_ready.set()
                with suppress(Timeout):
                    lock.acquire_write(timeout=2)
                release_thread.wait(timeout=5)

            thread_a = threading.Thread(target=target_a)
            thread_a.start()
            try:
                thread_ready.wait(timeout=2)
                time.sleep(0.05)
                with pytest.raises(Timeout):
                    lock.acquire_write(timeout=0.1)
            finally:
                release_thread.set()
                thread_a.join(timeout=5)
        finally:
            lock.close()
    finally:
        peer.release()
        peer.close()


@pytest.mark.timeout(10)
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
            barrier.wait(timeout=2)
            with lock.read_lock(timeout=5):
                time.sleep(0.05)

        threads = [threading.Thread(target=target) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)
    finally:
        lock.close()


@pytest.mark.timeout(10)
def test_timeout_raises(lock_file: str) -> None:
    held, release = Event(), Event()
    holder = Process(target=_worker, args=(lock_file, "write", held, release))
    with _cleanup([holder]):
        holder.start()
        assert held.wait(timeout=5)
        lock = _make_lock(lock_file)
        try:
            with pytest.raises(Timeout):
                lock.acquire_write(timeout=0.3)
        finally:
            lock.close()
        release.set()
        holder.join(timeout=5)


@pytest.mark.timeout(10)
def test_non_blocking_writer_contended_raises(lock_file: str) -> None:
    held, release = Event(), Event()
    holder = Process(target=_worker, args=(lock_file, "write", held, release))
    with _cleanup([holder]):
        holder.start()
        assert held.wait(timeout=5)
        lock = _make_lock(lock_file)
        try:
            with pytest.raises(Timeout):
                lock.acquire_write(timeout=1, blocking=False)
            with pytest.raises(Timeout):
                lock.acquire_read(timeout=1, blocking=False)
        finally:
            lock.close()
        release.set()
        holder.join(timeout=5)


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
        assert not Path(f"{lock_file}.write").exists()
    finally:
        reader.release()
        reader.close()


@_REQUIRES_POSIX_SIGNALS
@pytest.mark.timeout(20)
def test_dead_writer_evicted_by_reader(lock_file: str) -> None:
    import signal

    held = Event()
    holder = Process(target=_sigkill_worker, args=(lock_file, "write", held, 0.1, 0.5))
    with _cleanup([holder]):
        holder.start()
        assert held.wait(timeout=5)
        pid = holder.pid
        assert pid is not None
        os.kill(pid, getattr(signal, "SIGKILL"))  # noqa: B009 - signal.SIGKILL is POSIX-only
        holder.join(timeout=5)
        time.sleep(0.8)
        lock = _make_lock(lock_file)
        try:
            with lock.read_lock(timeout=5):
                pass
        finally:
            lock.close()
        assert not Path(f"{lock_file}.write").exists()


@_REQUIRES_POSIX_SIGNALS
@pytest.mark.timeout(20)
def test_dead_reader_evicted_by_writer(lock_file: str) -> None:
    import signal

    held = Event()
    holder = Process(target=_sigkill_worker, args=(lock_file, "read", held, 0.1, 0.5))
    with _cleanup([holder]):
        holder.start()
        assert held.wait(timeout=5)
        pid = holder.pid
        assert pid is not None
        os.kill(pid, getattr(signal, "SIGKILL"))  # noqa: B009 - signal.SIGKILL is POSIX-only
        holder.join(timeout=5)
        time.sleep(0.8)
        lock = _make_lock(lock_file)
        try:
            with lock.write_lock(timeout=5):
                pass
        finally:
            lock.close()


def test_heartbeat_self_stops_when_marker_vanishes(lock_file: str) -> None:
    lock = _make_lock(lock_file, heartbeat_interval=0.05, stale_threshold=0.2)
    lock.acquire_write(timeout=2)
    try:
        Path(f"{lock_file}.write").unlink()
        time.sleep(0.15)  # two-plus heartbeat ticks to observe the vanished marker and self-stop
    finally:
        lock.release(force=True)
        lock.close()
    # The vanished marker leaves nothing to evict, so a peer can acquire.
    peer = _make_lock(lock_file, heartbeat_interval=0.05, stale_threshold=0.2)
    try:
        with peer.write_lock(timeout=1):
            pass
    finally:
        peer.close()


def test_heartbeat_self_stops_on_token_replacement(lock_file: str) -> None:
    lock = _make_lock(lock_file, heartbeat_interval=0.05, stale_threshold=0.2)
    lock.acquire_write(timeout=2)
    try:
        # A well-formed marker holding a different token.
        Path(f"{lock_file}.write").write_bytes(b"0" * 32 + b"\n1\nhost\n")
        time.sleep(0.15)
    finally:
        lock.release(force=True)
        lock.close()


def test_release_keeps_a_peers_writer_marker(lock_file: str) -> None:
    # A holder paused past the stale threshold (GC pause, SIGSTOP, suspended VM) can have its marker evicted
    # by a peer that then claims the writer slot. On release the holder must not unlink that peer's live
    # marker; unlinking it would let a second writer through and break mutual exclusion.
    lock = _make_lock(lock_file, heartbeat_interval=10, stale_threshold=40)
    lock.acquire_write(timeout=2)
    try:
        write_marker = f"{lock_file}.write"
        peer_marker = b"a" * 32 + b"\n1\npeerhost\n"
        Path(write_marker).write_bytes(peer_marker)
        lock.release()
        assert Path(write_marker).read_bytes() == peer_marker
    finally:
        lock.close()


def test_writer_phase2_does_not_complete_on_a_peers_marker(lock_file: str, monkeypatch: pytest.MonkeyPatch) -> None:
    # A writer paused past stale_threshold during phase 2 (waiting for readers to drain) can have its stale
    # marker evicted by a peer that reclaims .write with its own token. Phase 2 must notice the foreign marker
    # rather than keep touching it and completing the acquire as if we still held the slot, which would let two
    # writers run at once. A live reader keeps phase 2 looping; on the first poll sleep we simulate the eviction
    # by overwriting .write with a peer token and draining the reader.
    reader = _make_lock(lock_file, heartbeat_interval=10, stale_threshold=40)
    reader.acquire_read(timeout=2)
    writer = _make_lock(lock_file, heartbeat_interval=10, stale_threshold=40)
    write_marker = f"{lock_file}.write"
    peer_marker = b"a" * 32 + b"\n1\npeerhost\n"
    real_sleep = time.sleep
    swapped = threading.Event()

    def hook(seconds: float) -> None:  # noqa: ARG001
        if not swapped.is_set():
            swapped.set()
            Path(write_marker).write_bytes(peer_marker)
            reader.release()
        real_sleep(0.005)

    monkeypatch.setattr(sync_mod.time, "sleep", hook)
    try:
        with pytest.raises(Timeout):
            writer.acquire_write(timeout=0.6)
        assert swapped.is_set()
        # We never overwrote or refreshed the peer's live marker.
        assert Path(write_marker).read_bytes() == peer_marker
    finally:
        monkeypatch.setattr(sync_mod.time, "sleep", real_sleep)
        writer.close()
        reader.close()


def test_heartbeat_survives_transient_touch_error(lock_file: str, monkeypatch: pytest.MonkeyPatch) -> None:
    # On the NFS-style filesystems this lock targets, a transient ESTALE/EIO on the heartbeat touch is
    # routine; it must not kill the heartbeat and drop the lease while we still believe we hold it.
    def boom(name: str, *, fd: int | None = None) -> None:  # noqa: ARG001
        raise OSError(EIO, "Input/output error")

    lock = _make_lock(lock_file, heartbeat_interval=0.02, stale_threshold=0.2)
    lock.acquire_write(timeout=2)
    try:
        hold = lock._hold
        assert hold is not None
        monkeypatch.setattr(sync_mod, "touch", boom)
        time.sleep(0.2)  # ~10 ticks, every one failing the touch
        assert hold.heartbeat_thread.is_alive()
        assert not hold.heartbeat_stop.is_set()
    finally:
        lock.release(force=True)
        lock.close()


@pytest.mark.parametrize(
    "target", [pytest.param("_open_marker_fd", id="open"), pytest.param("_read_marker_fd", id="read")]
)
def test_heartbeat_survives_a_transient_marker_error(
    lock_file: str, monkeypatch: pytest.MonkeyPatch, target: str
) -> None:
    # A transient ESTALE/EIO opening or reading the marker is routine on the NFS-style filesystems this lock targets.
    # Unlike the marker actually vanishing, it must not stop the heartbeat and drop the lease.
    def boom(*_args: object, **_kwargs: object) -> None:
        raise OSError(EIO, "Input/output error")

    lock = _make_lock(lock_file, heartbeat_interval=0.02, stale_threshold=0.2)
    lock.acquire_write(timeout=2)
    try:
        hold = lock._hold
        assert hold is not None
        monkeypatch.setattr(sync_mod, target, boom)
        time.sleep(0.2)  # ~10 ticks, every one failing the open or read
        assert hold.heartbeat_thread.is_alive()
        assert not hold.heartbeat_stop.is_set()
    finally:
        lock.release(force=True)
        lock.close()


def test_heartbeat_stops_when_marker_evicted(lock_file: str, monkeypatch: pytest.MonkeyPatch) -> None:
    # An O_NOFOLLOW open failing with ENOENT means the marker we held is gone: a peer evicted us. Unlike a transient
    # filesystem error, this is an unambiguous loss, so the heartbeat must stop rather than keep retrying.
    def gone(name: str, *, dir_fd: int | None = None) -> int:  # noqa: ARG001
        raise FileNotFoundError(ENOENT, "No such file or directory")

    lock = _make_lock(lock_file, heartbeat_interval=0.02, stale_threshold=0.2)
    lock.acquire_write(timeout=2)
    try:
        hold = lock._hold
        assert hold is not None
        monkeypatch.setattr(sync_mod, "_open_marker_fd", gone)
        assert hold.heartbeat_stop.wait(timeout=2)
        hold.heartbeat_thread.join(timeout=2)
        assert not hold.heartbeat_thread.is_alive()
    finally:
        lock.release(force=True)
        lock.close()


@pytest.mark.timeout(15)
def test_live_heartbeat_keeps_lock_alive_past_stale_threshold(lock_file: str) -> None:
    # Generous timing here so the test stays stable on slow Windows runners where the holder's
    # multiprocessing.spawn startup, the heartbeat thread scheduling, and the parent's mtime resolution
    # can all introduce sub-second jitter.
    heartbeat, stale = 0.3, 1.5
    held, release = Event(), Event()
    holder = Process(
        target=_worker,
        args=(lock_file, "write", held, release, -1, True, heartbeat, stale, 0.05),
    )
    with _cleanup([holder]):
        holder.start()
        assert held.wait(timeout=5)
        time.sleep(stale * 2)
        lock = _make_lock(lock_file, heartbeat_interval=heartbeat, stale_threshold=stale)
        try:
            with pytest.raises(Timeout):
                lock.acquire_write(timeout=0.5)
        finally:
            lock.close()
        release.set()
        holder.join(timeout=5)


@pytest.mark.parametrize(
    "content",
    [
        pytest.param(b"deadbeefdeadbeefdeadbeefdeadbeef\nnotanumber\nhost\n", id="non-numeric-pid"),
        pytest.param(b"deadbeefdeadbeefdeadbeefdeadbeef\n0\nhost\n", id="zero-pid"),
        pytest.param(b"deadbeefdeadbeefdeadbeefdeadbeef\n9999999999\nhost\n", id="pid-too-large"),
        pytest.param(b"bogus\n4711\nhost\n", id="wrong-length-token"),
        pytest.param(b"ZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZ\n4711\nhost\n", id="non-hex-token"),
        pytest.param(b"deadbeefdeadbeefdeadbeefdeadbeef\n4711\nhost with space\n", id="hostname-space"),
        pytest.param(b"deadbeefdeadbeefdeadbeefdeadbeef\n4711\nhost\n\n\n", id="trailing-blank-lines"),
        pytest.param(b"only one line\n", id="too-few-lines"),
        pytest.param(b"a\nb\nc\nd\n", id="too-many-lines"),
        pytest.param(b"x" * 2048, id="oversized"),
        pytest.param("ééé\n4711\nhost\n".encode(), id="non-ascii"),
    ],
)
def test_stale_malformed_marker_is_evicted(lock_file: str, content: bytes) -> None:
    _write_stale_marker(f"{lock_file}.write", content)
    lock = _make_lock(lock_file)
    try:
        with lock.write_lock(timeout=2):
            pass
    finally:
        lock.close()


def test_fifo_write_marker_does_not_block(lock_file: str) -> None:
    if sys.platform == "win32":
        pytest.skip("os.mkfifo is unix-only")  # also narrows sys.platform so ty resolves os.mkfifo below
    marker = f"{lock_file}.write"
    os.mkfifo(marker)
    past = time.time() - 1000
    os.utime(marker, (past, past))
    # Without O_NONBLOCK this open blocks forever; the lock instead reads the FIFO as a stale marker and evicts it.
    lock = _make_lock(lock_file)
    try:
        with lock.write_lock(timeout=2):
            pass
    finally:
        lock.close()


def test_fifo_write_marker_with_writer_is_evicted(lock_file: str) -> None:
    if sys.platform == "win32":
        pytest.skip("os.mkfifo is unix-only")  # also narrows sys.platform so ty resolves os.mkfifo below
    marker = f"{lock_file}.write"
    os.mkfifo(marker)
    past = time.time() - 1000
    os.utime(marker, (past, past))
    # A writer attached to the FIFO makes the non-blocking read raise EAGAIN on all platforms, matching a
    # writerless FIFO on FreeBSD (#587). The lock must evict the stale marker by mtime without reading it, so
    # the acquire completes instead of timing out.
    reader_fd = os.open(marker, os.O_RDONLY | os.O_NONBLOCK)
    writer_fd = os.open(marker, os.O_WRONLY)
    lock = _make_lock(lock_file)
    try:
        with lock.write_lock(timeout=2):
            pass
    finally:
        lock.close()
        os.close(writer_fd)
        os.close(reader_fd)


@pytest.mark.skipif(not hasattr(os, "O_NOFOLLOW"), reason="O_NOFOLLOW required")
def test_symlinked_write_marker_is_refused(lock_file: str, tmp_path: Path) -> None:
    victim = tmp_path / "victim"
    victim.write_text("do-not-touch")
    Path(f"{lock_file}.write").symlink_to(victim)
    lock = _make_lock(lock_file)
    try:
        with pytest.raises((OSError, Timeout)):
            lock.acquire_write(timeout=0.5)
    finally:
        lock.close()
    assert victim.read_text() == "do-not-touch"


@pytest.mark.skipif(
    os.utime not in os.supports_follow_symlinks, reason="os.utime cannot refuse symlinks on this platform"
)
def test_touch_does_not_follow_symlink(lock_file: str, tmp_path: Path) -> None:
    # The phase-2 writer-drain touch refreshes the .write marker by path (no held fd); if a peer swaps a
    # symlink in, the touch must land on the link itself, not the file it points at.
    victim = tmp_path / "victim"
    victim.write_text("do-not-touch")
    past = time.time() - 1000
    os.utime(victim, (past, past))
    marker = Path(f"{lock_file}.write")
    marker.symlink_to(victim)

    util_mod.touch(str(marker))

    assert victim.stat().st_mtime == past
    assert victim.read_text() == "do-not-touch"


@pytest.mark.skipif(not util_mod._SUPPORTS_UTIME_FD, reason="os.utime cannot target an fd on this platform")
def test_refresh_touches_verified_fd_not_swapped_path(
    lock_file: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A peer can swap our marker for a symlink in the window after the heartbeat's O_NOFOLLOW open but before
    # the touch; because the refresh touches the verified fd, the swapped symlink's target stays untouched.
    victim = tmp_path / "victim"
    victim.write_text("do-not-touch")
    past = time.time() - 1000
    os.utime(victim, (past, past))

    lock = _make_lock(lock_file, heartbeat_interval=30, stale_threshold=90)
    lock.acquire_write(timeout=2)
    try:
        marker = Path(f"{lock_file}.write")
        real_open = sync_mod._open_marker

        def swap_after_open(name: str, *, dir_fd: int | None = None) -> int | None:
            fd = real_open(name, dir_fd=dir_fd)
            if fd is not None and Path(name) == marker:
                marker.unlink()
                marker.symlink_to(victim)
            return fd

        monkeypatch.setattr(sync_mod, "_open_marker", swap_after_open)
        assert lock._refresh_marker() is True
        assert victim.stat().st_mtime == past
        assert victim.read_text() == "do-not-touch"
    finally:
        lock.release(force=True)
        lock.close()


def test_symlinked_readers_directory_is_refused(lock_file: str, tmp_path: Path) -> None:
    victim_dir = tmp_path / "victim_dir"
    victim_dir.mkdir()
    Path(f"{lock_file}.readers").symlink_to(victim_dir)
    lock = _make_lock(lock_file)
    try:
        with pytest.raises(RuntimeError, match="not a directory or is a symlink"):
            lock.acquire_read(timeout=0.5)
    finally:
        lock.close()
    assert list(victim_dir.iterdir()) == []


def test_readers_path_as_regular_file_is_refused(lock_file: str) -> None:
    Path(f"{lock_file}.readers").write_bytes(b"x")
    lock = _make_lock(lock_file)
    try:
        with pytest.raises(RuntimeError, match="not a directory or is a symlink"):
            lock.acquire_read(timeout=0.5)
    finally:
        lock.close()


@_REQUIRES_POSIX_PERMISSIONS
def test_write_marker_is_created_with_0600(lock_file: str) -> None:
    lock = _make_lock(lock_file)
    try:
        with lock.write_lock(timeout=2):
            assert stat.S_IMODE(Path(f"{lock_file}.write").lstat().st_mode) == 0o600
    finally:
        lock.close()


@_REQUIRES_POSIX_PERMISSIONS
def test_readers_directory_is_created_with_0700(lock_file: str) -> None:
    lock = _make_lock(lock_file)
    try:
        with lock.read_lock(timeout=2):
            assert stat.S_IMODE(Path(f"{lock_file}.readers").lstat().st_mode) == 0o700
    finally:
        lock.close()


def test_writer_ignores_housekeeping_files_in_readers_dir(lock_file: str) -> None:
    # A writer's phase-2 drain scan must not mistake dotfiles or leftover .break.* files from aborted
    # evictions for live readers.
    readers = Path(f"{lock_file}.readers")
    readers.mkdir(mode=0o700, exist_ok=True)
    (readers / ".hidden").write_bytes(b"ignored")
    (readers / "stale.break.12345.abcdef").write_bytes(b"also ignored")
    lock = _make_lock(lock_file)
    try:
        with lock.write_lock(timeout=2):
            pass
    finally:
        lock.close()


@_REQUIRES_POSIX_PERMISSIONS
def test_reader_file_is_created_with_0600(lock_file: str) -> None:
    lock = _make_lock(lock_file)
    try:
        with lock.read_lock(timeout=2):
            entries = list(Path(f"{lock_file}.readers").iterdir())
            assert len(entries) == 1
            assert stat.S_IMODE(entries[0].lstat().st_mode) == 0o600
    finally:
        lock.close()


@_REQUIRES_FORK
@pytest.mark.timeout(15)
def test_child_cannot_reuse_parents_lock_instance(tmp_path: Path) -> None:
    ctx = mp.get_context("spawn")
    result, failure = ctx.Event(), ctx.Event()
    proc = ctx.Process(target=_reuse_inherited_lock, args=(str(tmp_path / "foo.lock"), result, failure))
    proc.start()
    proc.join(timeout=10)
    assert not failure.is_set()
    assert result.is_set()


@_REQUIRES_FORK
@pytest.mark.timeout(15)
def test_child_release_on_inherited_lock_is_silent(tmp_path: Path) -> None:
    ctx = mp.get_context("spawn")
    result, failure = ctx.Event(), ctx.Event()
    proc = ctx.Process(target=_release_inherited_lock, args=(str(tmp_path / "foo.lock"), result, failure))
    proc.start()
    proc.join(timeout=10)
    assert not failure.is_set()
    assert result.is_set()


@_REQUIRES_FORK
@pytest.mark.timeout(15)
def test_child_can_acquire_a_different_lock_after_fork(tmp_path: Path) -> None:
    ctx = mp.get_context("spawn")
    result, failure = ctx.Event(), ctx.Event()
    proc = ctx.Process(
        target=_reacquire_fresh_lock_in_child,
        args=(str(tmp_path / "parent.lock"), str(tmp_path / "child.lock"), result, failure),
    )
    proc.start()
    proc.join(timeout=10)
    assert not failure.is_set()
    assert result.is_set()


@_REQUIRES_FORK
@pytest.mark.timeout(15)
# Holding the write lock keeps the heartbeat thread alive, so this fork is necessarily from a multi-threaded
# process and Python 3.15 warns that it may deadlock. That is the scenario under test, and it is already safe:
# register_at_fork resets inherited state in the child (any child use raises "invalidated by fork()"). Expected.
@pytest.mark.filterwarnings("ignore:.*multi-threaded, use of fork.*:DeprecationWarning")
def test_parent_retains_lock_across_fork(tmp_path: Path) -> None:
    path = str(tmp_path / "foo.lock")
    lock = SoftReadWriteLock(path, heartbeat_interval=0.2, stale_threshold=1.0, poll_interval=0.02)
    lock.acquire_write(timeout=5)
    try:
        child = _fork_process(target=time.sleep, args=(0.05,))
        child.start()
        child.join(timeout=5)
        assert Path(f"{path}.write").exists()
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
    assert not Path(f"{path}.write").exists()


def _make_lock(
    path: str,
    *,
    heartbeat_interval: float = 0.1,
    stale_threshold: float = 0.5,
    poll_interval: float = 0.02,
    is_singleton: bool = False,
) -> SoftReadWriteLock:
    return SoftReadWriteLock(
        path,
        heartbeat_interval=heartbeat_interval,
        stale_threshold=stale_threshold,
        poll_interval=poll_interval,
        is_singleton=is_singleton,
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
                release_event.wait(timeout=10)
            else:
                time.sleep(0.2)
    finally:
        lock.close()


@contextmanager
def _cleanup(processes: list[Process]) -> Generator[None]:
    try:
        yield
    finally:
        for proc in processes:
            if proc.is_alive():
                proc.terminate()
                proc.join(timeout=2)


def _sigkill_worker(
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


def _write_stale_marker(path: str, content: bytes) -> None:
    Path(path).write_bytes(content)
    past = time.time() - 1000
    os.utime(path, (past, past))


def _reuse_inherited_lock(lock_file: str, result: EventType, failure: EventType) -> None:
    lock = SoftReadWriteLock(lock_file, heartbeat_interval=0.2, stale_threshold=1.0, poll_interval=0.02)
    lock.acquire_write(timeout=5)
    ok = _fork_event()

    def child_entry() -> None:
        try:
            lock.acquire_read(timeout=1)
        except RuntimeError as exc:
            if "invalidated by fork" in str(exc):
                ok.set()

    child = _fork_process(target=child_entry)
    child.start()
    child.join(timeout=5)
    if ok.is_set():
        result.set()
    else:
        failure.set()
    lock.release()
    lock.close()


def _release_inherited_lock(lock_file: str, result: EventType, failure: EventType) -> None:
    lock = SoftReadWriteLock(lock_file, heartbeat_interval=0.2, stale_threshold=1.0, poll_interval=0.02)
    lock.acquire_read(timeout=5)
    ok = _fork_event()

    def child_entry() -> None:
        try:
            lock.release()
        except RuntimeError:
            return
        ok.set()

    child = _fork_process(target=child_entry)
    child.start()
    child.join(timeout=5)
    if ok.is_set():
        result.set()
    else:
        failure.set()
    lock.release()
    lock.close()


def _reacquire_fresh_lock_in_child(lock_file: str, child_path: str, result: EventType, failure: EventType) -> None:
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
    child.join(timeout=5)
    if ok.is_set():
        result.set()
    else:
        failure.set()
    parent_lock.release()
    parent_lock.close()


def _fork_process(target: Callable[..., object], args: tuple[object, ...] = ()) -> mp.process.BaseProcess:
    if sys.platform == "win32":
        msg = "fork context is POSIX only"
        raise RuntimeError(msg)
    return mp.get_context("fork").Process(target=target, args=args)


def _fork_event() -> EventType:
    if sys.platform == "win32":
        msg = "fork context is POSIX only"
        raise RuntimeError(msg)
    return mp.get_context("fork").Event()


def test_write_marker_zero_write_rolls_back(lock_file: str, mocker: MockerFixture) -> None:
    mocker.patch("filelock._util.os.write", return_value=0)

    lock = SoftReadWriteLock(lock_file, is_singleton=False)
    with pytest.raises(OSError, match="0 bytes"):
        lock.acquire_write(timeout=1)
    assert not Path(f"{lock_file}.write").exists()
