from __future__ import annotations

import copy
import logging
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from queue import Queue
from typing import TYPE_CHECKING

import pytest

from filelock import ReleasePendingError, StrictSoftFileLock, Timeout

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from pytest_mock import MockerFixture
from .capability_marks import WINDOWS_ONLY


@WINDOWS_ONLY  # pragma: win32 cover
def test_busy_shared_gate_leaves_release_unstarted(lock_path: Path) -> None:
    lock = StrictSoftFileLock(lock_path, thread_local=False)
    with lock, ThreadPoolExecutor(1) as pool, lock._transition_lock:
        with pytest.raises(TimeoutError, match="waiting to start") as caught:
            pool.submit(lock.release_with_timeout, 0.02).result(5)
        assert (type(caught.value), lock.is_locked, len(lock.claims)) == (TimeoutError, True, 2)
    assert lock.claims == ()


@WINDOWS_ONLY  # pragma: win32 cover
def test_worker_reentrant_release_preserves_claim_until_withdrawal(lock_path: Path, mocker: MockerFixture) -> None:
    lock = StrictSoftFileLock(lock_path)
    lock.acquire()
    release = lock._release

    def reenter() -> None:
        with pytest.raises(RuntimeError, match="during this thread's lock transition"):
            lock.release_with_timeout(0)
        lock.release()
        assert (lock.is_locked, len(lock.claims)) == (True, 2)
        release()

    mocker.patch.object(lock, "_release", autospec=True, side_effect=reenter)
    lock.release_with_timeout(5)
    assert (lock.is_locked, lock.claims) == (False, ())


@WINDOWS_ONLY  # pragma: win32 cover
@pytest.mark.parametrize(
    "finish",
    [
        pytest.param(lambda lock: lock.release(), id="ordinary"),
        pytest.param(lambda lock: lock.release_with_timeout(5), id="timed"),
    ],
)
def test_queued_release_observes_the_same_withdrawal_error(
    lock_path: Path, mocker: MockerFixture, finish: Callable[[StrictSoftFileLock], None]
) -> None:
    lock = StrictSoftFileLock(lock_path, thread_local=False)
    lock.acquire()
    gate = lock._transition_lock
    queued, proceed = threading.Event(), threading.Event()

    def enter(blocking: bool = True, timeout: float = -1) -> bool:
        queued.set()
        return gate.acquire(blocking, timeout)

    waiting_gate = mocker.patch.object(lock, "_transition_lock", autospec=True, wraps=gate)
    waiting_gate.acquire.side_effect = enter
    waiting_gate.release.side_effect = gate.release
    waiting_gate.__enter__.side_effect = enter
    waiting_gate.__exit__.side_effect = gate.__exit__

    def refuse() -> None:
        assert proceed.wait(5)
        msg = "shared withdrawal refused"
        raise PermissionError(msg)

    refused = mocker.patch.object(lock, "_release", autospec=True, side_effect=refuse)
    with ThreadPoolExecutor(1) as pool:
        with gate:
            waiting = pool.submit(finish, lock)
            assert queued.wait(5)
            with pytest.raises(ReleasePendingError):
                lock.release_with_timeout(0)
        proceed.set()
        with pytest.raises(PermissionError, match="shared withdrawal refused"):
            waiting.result(5)
    assert (lock.is_locked, len(lock.claims)) == (True, 2)
    mocker.stop(refused)
    lock.release()
    assert lock.claims == ()


@WINDOWS_ONLY  # pragma: win32 cover
def test_overlapping_observers_keep_a_successors_pending_release(lock_path: Path, mocker: MockerFixture) -> None:
    lock = StrictSoftFileLock(lock_path, thread_local=False)
    lock.acquire()
    release = lock._release
    proceed = threading.Event()

    def pause() -> None:
        assert proceed.wait(5)
        release()

    mocker.patch.object(lock, "_release", autospec=True, side_effect=pause)
    with pytest.raises(ReleasePendingError):
        lock.release_with_timeout(0)
    observed, acknowledge = threading.Event(), threading.Event()
    wait = Future.exception

    def defer_receipt(receipt: Future[None], timeout: float | None = None) -> BaseException | None:
        error = wait(receipt, timeout)
        observed.set()
        assert acknowledge.wait(5)
        return error

    with ThreadPoolExecutor(1) as pool:
        delayed = mocker.patch.object(Future, "exception", autospec=True, side_effect=defer_receipt)
        previous = pool.submit(lock.release_with_timeout, 5)
        try:
            proceed.set()
            assert observed.wait(5)
            mocker.stop(delayed)
            lock.release()
            lock.acquire()
            proceed.clear()
            with pytest.raises(ReleasePendingError):
                lock.release_with_timeout(0)
            acknowledge.set()
            previous.result(5)
            with pytest.raises(Timeout):
                lock.acquire(timeout=0)
        finally:
            acknowledge.set()
            proceed.set()
    lock.release()
    assert (lock.is_locked, lock.claims) == (False, ())


@WINDOWS_ONLY  # pragma: win32 cover
def test_worker_withdraws_after_a_reentrant_start_callback(lock_path: Path, mocker: MockerFixture) -> None:
    lock = StrictSoftFileLock(lock_path)
    lock.acquire()
    start = threading.Thread.start
    releasers: list[threading.Thread] = []
    release = lock._release

    def withdraw() -> None:
        releasers.append(threading.current_thread())
        release()

    def reenter(worker: threading.Thread) -> None:
        start(worker)
        lock.release()
        assert (lock.is_locked, len(lock.claims)) == (True, 2)

    mocker.patch.object(lock, "_release", autospec=True, side_effect=withdraw)
    mocker.patch.object(threading.Thread, "start", autospec=True, side_effect=reenter)
    lock.release_with_timeout(5)
    (releaser,) = releasers
    assert (releaser is threading.current_thread(), lock.is_locked, lock.claims) == (False, False, ())


@WINDOWS_ONLY  # pragma: win32 cover
@pytest.mark.parametrize(
    ("message", "claims"),
    [
        pytest.param("Attempting to release lock %s on %s", 2, id="before-withdrawal"),
        pytest.param("Lock %s released on %s", 0, id="after-withdrawal"),
    ],
)
def test_log_callback_can_release_while_shared_observer_owns_gate(
    lock_path: Path, caplog: pytest.LogCaptureFixture, message: str, claims: int
) -> None:
    lock = StrictSoftFileLock(lock_path, thread_local=False)
    lock.acquire()
    seen: list[tuple[bool, int]] = []

    def reenter(record: logging.LogRecord) -> bool:
        if record.msg == message and record.args == (id(lock), str(lock_path)):
            lock.release()
            with pytest.raises(RuntimeError, match="cannot acquire during this thread's release"):
                lock.acquire(timeout=-1)
            with pytest.raises(RuntimeError, match="during this thread's lock transition"):
                lock.release_with_timeout(0)
            seen.append((lock.is_locked, len(lock.claims)))
        return True

    caplog.set_level(logging.DEBUG, logger="filelock")
    logger = logging.getLogger("filelock")
    logger.addFilter(reenter)
    try:
        with lock._transition_lock:
            lock.release_with_timeout(5)
    finally:
        logger.removeFilter(reenter)
    assert (seen, lock.is_locked, lock.claims) == ([(True, claims)], False, ())


@WINDOWS_ONLY  # pragma: win32 cover
def test_wait_on_previous_release_preserves_current_hold(lock_path: Path, mocker: MockerFixture) -> None:
    lock = StrictSoftFileLock(lock_path, thread_local=False)
    lock.acquire()
    proceed = threading.Event()
    release = lock._release

    def pause() -> None:
        assert proceed.wait(5)
        release()

    mocker.patch.object(lock, "_release", autospec=True, side_effect=pause)
    try:
        with pytest.raises(ReleasePendingError) as previous:
            lock.release_with_timeout(0)
        with pytest.raises(ReleasePendingError):
            previous.value.wait(0)
    finally:
        proceed.set()
    lock.release()
    with lock:
        previous.value.wait()
        assert (lock.is_locked, lock.lock_counter, len(lock.claims)) == (True, 1, 2)
    assert lock.claims == ()


def test_copied_pending_error_waits_for_the_same_operation() -> None:
    waits: list[float | None] = []
    receipt = copy.copy(ReleasePendingError("still releasing", waits.append))
    receipt.wait(0.5)
    assert (str(receipt), waits) == ("still releasing", [0.5])


@WINDOWS_ONLY  # pragma: win32 cover
def test_worker_cannot_wait_for_its_own_receipt(lock_path: Path, mocker: MockerFixture) -> None:
    lock = StrictSoftFileLock(lock_path)
    lock.acquire()
    receipts: Queue[ReleasePendingError] = Queue()
    release = lock._release

    def withdraw() -> None:
        receipt = receipts.get(timeout=5)
        with pytest.raises(RuntimeError, match="cannot wait for its own completion"):
            receipt.wait(1)
        release()

    mocker.patch.object(lock, "_release", autospec=True, side_effect=withdraw)
    with pytest.raises(ReleasePendingError) as pending:
        lock.release_with_timeout(0)
    receipts.put(pending.value)
    pending.value.wait(5)
    assert (lock.is_locked, lock.claims) == (False, ())
