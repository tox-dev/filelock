from __future__ import annotations

import multiprocessing as mp
import sys
import time
from multiprocessing import Process
from typing import TYPE_CHECKING, Final

import pytest

from filelock import SoftReadWriteLock, Timeout
from tests.capability_marks import (
    NEEDS_FORK,
    SKIP_ON_UNRELIABLE_PROCESS_SYNC,
)
from tests.process_helpers import cleanup_processes

from .lock_helpers import PROCESS_DEADLINE, read_state

if TYPE_CHECKING:
    from collections.abc import Callable
    from multiprocessing.synchronize import Event as EventType
    from pathlib import Path


pytestmark: Final[list[pytest.MarkDecorator]] = [
    pytest.mark.requires_hard_links,
    pytest.mark.usefixtures("clear_singletons"),
]


@SKIP_ON_UNRELIABLE_PROCESS_SYNC
@NEEDS_FORK
@pytest.mark.timeout(PROCESS_DEADLINE * 2)
@pytest.mark.parametrize(
    ("scenario", "names"),
    [
        pytest.param("reuse-raises", ("foo.lock",), id="reuse-raises"),
        pytest.param("release-is-silent", ("foo.lock",), id="release-is-silent"),
        pytest.param("fresh-lock-after-fork", ("parent.lock", "child.lock"), id="fresh-lock-after-fork"),
    ],
)
def test_child_fork_lock_behavior(tmp_path: Path, scenario: str, names: tuple[str, ...]) -> None:  # pragma: needs fork
    # The decorator runs before these process targets are defined.
    target = {
        "reuse-raises": _reuse_inherited_lock,
        "release-is-silent": _release_inherited_lock,
        "fresh-lock-after-fork": _reacquire_fresh_lock_in_child,
    }[scenario]
    ctx = mp.get_context("spawn")
    result, failure = ctx.Event(), ctx.Event()
    proc = ctx.Process(target=target, args=(*(str(tmp_path / name) for name in names), result, failure))
    with cleanup_processes([proc]):
        proc.start()
        proc.join(timeout=PROCESS_DEADLINE)
        assert not failure.is_set()
        assert result.is_set()


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
    child.join(timeout=PROCESS_DEADLINE)
    if ok.is_set():
        result.set()
    else:  # pragma: no cover  # reached only if the child fails to report the inherited lock, i.e. a regression
        failure.set()
    lock.release()
    lock.close()


def _fork_event() -> EventType:  # pragma: needs fork
    if sys.platform == "win32":  # pragma: win32 cover
        msg = "fork context is POSIX only"
        raise RuntimeError(msg)
    return mp.get_context("fork").Event()


def _fork_process(  # pragma: needs fork
    target: Callable[[], object] | Callable[[float], object], args: tuple[float, ...] = ()
) -> mp.process.BaseProcess:
    if sys.platform == "win32":  # pragma: win32 cover
        msg = "fork context is POSIX only"
        raise RuntimeError(msg)
    return mp.get_context("fork").Process(target=target, args=args)


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
    child.join(timeout=PROCESS_DEADLINE)
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
    child.join(timeout=PROCESS_DEADLINE)
    if ok.is_set():
        result.set()
    else:  # pragma: no cover  # reached only if the child cannot acquire a fresh lock after the fork, i.e. a regression
        failure.set()
    parent_lock.release()
    parent_lock.close()


@NEEDS_FORK
@pytest.mark.timeout(PROCESS_DEADLINE * 2)
# The heartbeat keeps a thread alive; this test exercises fork during a live hold.
@SKIP_ON_UNRELIABLE_PROCESS_SYNC
@pytest.mark.filterwarnings("ignore:.*multi-threaded, use of fork.*:DeprecationWarning")
def test_parent_retains_lock_across_fork(tmp_path: Path) -> None:  # pragma: needs fork
    path = str(tmp_path / "foo.lock")
    lock = SoftReadWriteLock(path, heartbeat_interval=0.2, stale_threshold=1.0, poll_interval=0.02)
    lock.acquire_write(timeout=5)
    try:
        child = _fork_process(target=time.sleep, args=(0.05,))
        child.start()
        child.join(timeout=PROCESS_DEADLINE)
        assert read_state(path).writer is not None
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
    assert read_state(path).writer is None


@SKIP_ON_UNRELIABLE_PROCESS_SYNC
def test_cleanup_terminates_a_still_running_process() -> None:
    proc = Process(target=time.sleep, args=(30,))
    proc.start()
    started = time.monotonic()

    with cleanup_processes([proc]):
        assert proc.is_alive()

    # The child sleeps longer than the join deadline.
    assert time.monotonic() - started < 10


@SKIP_ON_UNRELIABLE_PROCESS_SYNC
def test_cleanup_closes_the_process() -> None:
    proc = Process(target=time.sleep, args=(30,))
    proc.start()

    with cleanup_processes([proc]):
        assert proc.is_alive()

    with pytest.raises(ValueError, match="process object is closed"):
        proc.is_alive()
