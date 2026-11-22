from __future__ import annotations

import signal
import time
from contextlib import closing
from multiprocessing import Event, Process, Value, get_context
from typing import TYPE_CHECKING, Final, Literal

import pytest

pytest.importorskip("sqlite3")
from filelock import ReadWriteLock, Timeout
from tests.capability_marks import (
    NEEDS_POSIX_SIGNALS,
    SKIP_ON_UNRELIABLE_PROCESS_SYNC,
)
from tests.process_helpers import cleanup_processes
from tests.read_write_helpers import assert_read_write_lock_state
from tests.sqlite.process_support import PROCESS_DEADLINE, acquire_lock

if TYPE_CHECKING:
    from multiprocessing.sharedctypes import Synchronized
    from multiprocessing.synchronize import Event as EventType
    from types import FrameType


pytestmark: Final[pytest.MarkDecorator] = SKIP_ON_UNRELIABLE_PROCESS_SYNC


@pytest.mark.timeout(PROCESS_DEADLINE * 5)
def test_read_locks_are_shared(lock_file: str) -> None:
    read1_acquired = Event()
    read2_acquired = Event()
    release_read1 = Event()
    release_read2 = Event()

    reader1 = Process(target=acquire_lock, args=(lock_file, "read", read1_acquired, release_read1))
    reader2 = Process(target=acquire_lock, args=(lock_file, "read", read2_acquired, release_read2))

    with cleanup_processes([reader1, reader2]):
        reader1.start()
        assert read1_acquired.wait(timeout=PROCESS_DEADLINE), f"First read lock not acquired on {lock_file}"
        reader2.start()

        assert read2_acquired.wait(timeout=PROCESS_DEADLINE), f"Second read lock not acquired on {lock_file}"

        release_read1.set()
        release_read2.set()
        reader1.join(timeout=PROCESS_DEADLINE)
        reader2.join(timeout=PROCESS_DEADLINE)
        assert not reader1.is_alive(), "Reader 1 did not exit cleanly"
        assert not reader2.is_alive(), "Reader 2 did not exit cleanly"


@pytest.mark.timeout(PROCESS_DEADLINE * 5)
def test_write_lock_excludes_other_write_locks(lock_file: str) -> None:
    write1_acquired = Event()
    release_write1 = Event()
    write2_acquired = Event()

    holder = Process(target=acquire_lock, args=(lock_file, "write", write1_acquired, release_write1))
    contender = Process(target=acquire_lock, args=(lock_file, "write", write2_acquired, None, 0.5, True))

    with cleanup_processes([holder]):
        holder.start()
        assert write1_acquired.wait(timeout=PROCESS_DEADLINE), "First write lock not acquired"

        with cleanup_processes([contender]):
            contender.start()
            # Wait for the contender's own timeout rather than release behind its back: a slow start could otherwise
            # let it acquire after the release and be terminated inside the event's lock, which then never unlocks.
            contender.join(timeout=PROCESS_DEADLINE)
            assert (contender.is_alive(), write2_acquired.is_set()) == (False, False), (
                "Second write lock should not be acquired"
            )

        release_write1.set()
        holder.join(timeout=PROCESS_DEADLINE)
        assert not holder.is_alive(), "Holder did not exit cleanly"

        successor = Process(target=acquire_lock, args=(lock_file, "write", write2_acquired))

        with cleanup_processes([successor]):
            successor.start()
            assert write2_acquired.wait(timeout=PROCESS_DEADLINE), "Second write lock not acquired after first released"
            successor.join(timeout=PROCESS_DEADLINE)
            assert not successor.is_alive(), "Successor did not exit cleanly"


@pytest.mark.parametrize(
    ("holder_mode", "contender_mode"),
    [
        pytest.param("write", "read", id="write-blocks-read"),
        pytest.param("read", "write", id="read-blocks-write"),
    ],
)
@pytest.mark.timeout(PROCESS_DEADLINE * 5)
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
        assert holder_acquired.wait(timeout=PROCESS_DEADLINE), f"{holder_mode} lock not acquired"

        contender.start()
        contender_started.set()

        time.sleep(2)  # confirm the contender stays blocked  # simplint: ignore[sleep-in-test]  # no blocked signal
        assert not contender_acquired.is_set(), f"{contender_mode} lock should not be acquired while {holder_mode} held"

        release_holder.set()
        holder.join(timeout=PROCESS_DEADLINE)

        assert contender_acquired.wait(timeout=PROCESS_DEADLINE), (
            f"{contender_mode} lock not acquired after {holder_mode} released"
        )

        contender.join(timeout=PROCESS_DEADLINE)
        assert not contender.is_alive(), "Contender did not exit cleanly"


@pytest.mark.timeout(PROCESS_DEADLINE * 8)
def test_write_non_starvation(lock_file: str) -> None:
    NUM_EARLY_READERS: Final[int] = 2
    NUM_LATE_READERS: Final[int] = 5

    early_acquired = [Event() for _ in range(NUM_EARLY_READERS)]
    early_release = [Event() for _ in range(NUM_EARLY_READERS)]
    late_announced = [Event() for _ in range(NUM_LATE_READERS)]
    writer_contending = Event()
    writer_acquired = Event()
    writer_release = Event()
    late_entries = Value("i", 0)

    early = [
        Process(target=acquire_lock, args=(lock_file, "read", early_acquired[idx], early_release[idx]))
        for idx in range(NUM_EARLY_READERS)
    ]
    late = [
        Process(target=_late_reader, args=(lock_file, late_announced[idx], late_entries))
        for idx in range(NUM_LATE_READERS)
    ]
    writer = Process(
        target=acquire_lock,
        args=(
            lock_file,
            "write",
            writer_acquired,
            writer_release,
            PROCESS_DEADLINE * 2,
            True,
            None,
            writer_contending,
        ),
    )

    prober = ReadWriteLock(lock_file, is_singleton=False)
    with cleanup_processes([*early, *late, writer]):
        for reader in early:
            reader.start()
        for acquired in early_acquired:
            assert acquired.wait(timeout=PROCESS_DEADLINE), "Early reader did not acquire lock"
        assert _write_intent_pending(prober, time.monotonic() + PROCESS_DEADLINE)

        writer.start()
        assert writer_contending.wait(timeout=PROCESS_DEADLINE), "Writer process did not reach the lock"
        probe_deadline = time.monotonic() + PROCESS_DEADLINE
        while _write_intent_pending(prober, probe_deadline):  # pragma: no branch  # exits once the intent blocks reads
            pass  # pragma: no cover  # runs only when the first probe precedes the writer's intent
        prober.close()
        # A held write lock refuses the probe too, so only a writer still waiting proves its intent blocks readers.
        assert not writer_acquired.is_set(), "Probe was refused by the held write lock, not by a pending intent"

        for reader in late:
            reader.start()
        for announced in late_announced:
            assert announced.wait(timeout=PROCESS_DEADLINE), "Late reader did not start"

        for release in early_release:
            release.set()
        assert writer_acquired.wait(timeout=PROCESS_DEADLINE * 2), "Writer couldn't acquire lock - possible starvation"
        with late_entries.get_lock():
            admitted = late_entries.value
        assert admitted == 0, f"{admitted} readers arrived after the writer and got in before it"

        writer_release.set()
        writer.join(timeout=PROCESS_DEADLINE)
        assert not writer.is_alive(), "Writer did not exit cleanly"
        for idx, reader in enumerate([*early, *late]):
            reader.join(timeout=PROCESS_DEADLINE)
            assert not reader.is_alive(), f"Reader {idx} did not exit cleanly"


def _late_reader(lock_file: str, announced: EventType, entries: Synchronized[int]) -> None:
    announced.set()
    with ReadWriteLock(lock_file).read_lock(), entries.get_lock():
        entries.value += 1


def _write_intent_pending(prober: ReadWriteLock, deadline: float) -> bool:
    try:
        prober.acquire_read(blocking=False)
    except Timeout:
        return False
    prober.release()
    assert time.monotonic() < deadline, "Writer never registered its write intent"
    time.sleep(0.05)  # simplint: ignore[sleep-in-test]  # needs a write-intent signal
    return True


@NEEDS_POSIX_SIGNALS  # pragma: needs posix-signals
@pytest.mark.timeout(PROCESS_DEADLINE * 3)
def test_single_threaded_writer_handles_signal_while_readers_hold_lock(lock_file: str) -> None:
    context = get_context("spawn")
    held, release = context.Event(), context.Event()
    reader = context.Process(target=acquire_lock, args=(lock_file, "read", held, release))
    waiter = context.Process(target=_interrupt_waiting_writer, args=(lock_file,))
    with cleanup_processes([reader, waiter]), closing(ReadWriteLock(lock_file)) as prober:
        reader.start()
        assert held.wait(PROCESS_DEADLINE)
        waiter.start()
        deadline = time.monotonic() + PROCESS_DEADLINE
        while _write_intent_pending(prober, deadline):
            pass
        waiter.terminate()
        waiter.join(timeout=5)
        assert waiter.exitcode == 0
        release.set()


def _interrupt_waiting_writer(lock_file: str) -> None:  # pragma: needs posix-signals
    with closing(ReadWriteLock(lock_file)) as waiter, closing(ReadWriteLock(lock_file, is_singleton=False)) as peer:

        def stop(_signum: int, _frame: FrameType | None) -> None:
            with pytest.raises(Timeout):
                peer.acquire_read(blocking=False)
            raise KeyboardInterrupt

        signal.signal(signal.SIGTERM, stop)
        with pytest.raises(KeyboardInterrupt):
            waiter.acquire_write()
        with pytest.raises(RuntimeError, match="not held"):
            waiter.release()
        peer.acquire_read(blocking=False)
        peer.release()


def test_writer_timeout_releases_pending_lock(lock_file: str) -> None:
    with ReadWriteLock(lock_file).read_lock(), closing(ReadWriteLock(lock_file, is_singleton=False)) as writer:
        with pytest.raises(Timeout):
            writer.acquire_write(timeout=0.25)
        with pytest.raises(RuntimeError, match="not held"):
            writer.release()
        assert_read_write_lock_state(lock_file, "read", available=True)
