from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from functools import partial
from typing import TYPE_CHECKING, Final, Literal

import pytest

from filelock import SoftReadWriteLock, Timeout
from filelock._soft._read_write._generations import GenerationLog, Snapshot
from filelock._soft._read_write._storage import OsFiles

from .lock_helpers import PROCESS_DEADLINE, make_lock, read_state, writer_gets_in

if TYPE_CHECKING:
    from collections.abc import Callable, Generator


pytestmark: Final[list[pytest.MarkDecorator]] = [
    pytest.mark.requires_hard_links,
    pytest.mark.usefixtures("clear_singletons"),
]


def test_reentrant_read_holds_and_releases(lock_file: str, lock: SoftReadWriteLock) -> None:
    with lock.read_lock(timeout=2):
        with lock.read_lock(timeout=2):
            pass
        assert not writer_gets_in(lock_file)
    assert writer_gets_in(lock_file)


def test_reentrant_write_holds_and_releases(lock_file: str, lock: SoftReadWriteLock) -> None:
    with lock.write_lock(timeout=2), lock.write_lock(timeout=2):
        assert read_state(lock_file).writer is not None
    assert read_state(lock_file).writer is None


@pytest.mark.parametrize(
    ("first_mode", "second_mode", "direction"),
    [
        pytest.param("read", "write", "upgrade", id="upgrade"),
        pytest.param("write", "read", "downgrade", id="downgrade"),
    ],
)
def test_change_of_mode_while_held_raises(
    lock: SoftReadWriteLock, first_mode: Literal["read", "write"], second_mode: Literal["read", "write"], direction: str
) -> None:
    first_lock = getattr(lock, f"{first_mode}_lock")
    second_acquire = getattr(lock, f"acquire_{second_mode}")
    with first_lock(timeout=2), pytest.raises(RuntimeError, match=f"{direction} not allowed"):
        second_acquire(timeout=1)


def test_write_lock_is_thread_pinned(lock: SoftReadWriteLock) -> None:
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
    assert len(errors) == 1
    assert isinstance(errors[0], (RuntimeError, Timeout))


def test_write_lock_release_from_different_thread_prohibited(lock_file: str, lock: SoftReadWriteLock) -> None:
    with closing(make_lock(lock_file)) as contender, ThreadPoolExecutor(max_workers=1) as other_thread:
        lock.acquire_write(timeout=2)
        with pytest.raises(RuntimeError, match="while it is held by thread"):
            other_thread.submit(lock.release).result()
        with pytest.raises(Timeout):
            contender.acquire_write(blocking=False)


@pytest.mark.parametrize(
    "drop",
    [
        pytest.param(SoftReadWriteLock.close, id="close"),
        pytest.param(partial(SoftReadWriteLock.release, force=True), id="force-release"),
    ],
)
def test_write_lock_drop_from_different_thread_needs_an_explicit_override(
    lock_file: str, lock: SoftReadWriteLock, drop: Callable[[SoftReadWriteLock], None]
) -> None:
    with ThreadPoolExecutor(max_workers=1) as other_thread:
        lock.acquire_write(timeout=2)
        other_thread.submit(drop, lock).result()
        assert writer_gets_in(lock_file)


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
        assert lock.generation is None
    finally:
        lock.close()


def test_release_force_on_reentrant_lock_drops_all(lock: SoftReadWriteLock) -> None:
    lock.acquire_read(timeout=2)
    lock.acquire_read(timeout=2)
    lock.release(force=True)
    assert lock.generation is None
    with lock.write_lock(timeout=2):
        assert lock.generation is not None


def test_soft_read_on_a_lost_hold_raises(lost_lock: SoftReadWriteLock, lost: threading.Event) -> None:
    lost_lock.acquire_read(timeout=2)
    _evict_every_holder(lost_lock.lock_file)
    assert lost.wait(PROCESS_DEADLINE)

    with ThreadPoolExecutor(max_workers=1) as other_thread, pytest.raises(RuntimeError, match="hold was lost"):
        other_thread.submit(lost_lock.acquire_read, 1).result(PROCESS_DEADLINE)
    lost_lock.release()


def _evict_every_holder(lock_file: str) -> None:
    log = GenerationLog(OsFiles(lock_file), lock_file, f"{lock_file}.rw")
    assert log.commit(Snapshot(generation=log.latest().generation + 1, writer=None, readers=frozenset()))


def test_soft_read_after_the_lost_hold_drains_asks_the_log(lost_lock: SoftReadWriteLock, lost: threading.Event) -> None:
    lost_lock.acquire_read(timeout=2)
    _evict_every_holder(lost_lock.lock_file)
    assert lost.wait(PROCESS_DEADLINE)
    lost_lock.release()
    with closing(make_lock(lost_lock.lock_file, heartbeat_interval=0.05, stale_threshold=0.2)) as writer:
        writer.acquire_write(timeout=2)

        with pytest.raises(Timeout):
            lost_lock.acquire_read(blocking=False)


@pytest.fixture
def lost() -> threading.Event:
    return threading.Event()


@pytest.fixture
def lost_lock(lock_file: str, lost: threading.Event) -> Generator[SoftReadWriteLock]:
    instance = make_lock(
        lock_file, heartbeat_interval=0.05, stale_threshold=0.2, on_compromise=lambda _compromise: lost.set()
    )
    try:
        yield instance
    finally:
        instance.close()


def test_soft_read_release_from_a_thread_without_a_read_level_raises(
    lock: SoftReadWriteLock, other_thread: ThreadPoolExecutor
) -> None:
    lock.acquire_read(timeout=2)

    with pytest.raises(RuntimeError, match="which does not hold it"):
        other_thread.submit(lock.release).result(PROCESS_DEADLINE)

    assert not writer_gets_in(lock.lock_file)
    lock.release()


def test_soft_read_release_of_a_dropped_hold_leaves_a_newer_one_held(
    lock: SoftReadWriteLock, other_thread: ThreadPoolExecutor
) -> None:
    lock.acquire_read(timeout=2)
    lock.release(force=True)
    other_thread.submit(lock.acquire_read, 2).result(PROCESS_DEADLINE)

    with pytest.raises(RuntimeError, match="which does not hold it"):
        lock.release()

    assert not writer_gets_in(lock.lock_file)
    other_thread.submit(lock.release).result(PROCESS_DEADLINE)


def test_soft_read_release_gives_back_only_the_callers_levels(
    lock: SoftReadWriteLock, other_thread: ThreadPoolExecutor
) -> None:
    lock.acquire_read(timeout=2)
    other_thread.submit(lock.acquire_read, 2).result(PROCESS_DEADLINE)
    lock.release()

    with pytest.raises(RuntimeError, match="which does not hold it"):
        lock.release()

    assert not writer_gets_in(lock.lock_file)
    other_thread.submit(lock.release).result(PROCESS_DEADLINE)
    assert writer_gets_in(lock.lock_file)


@pytest.fixture
def other_thread() -> Generator[ThreadPoolExecutor]:
    with ThreadPoolExecutor(max_workers=1) as executor:
        yield executor
