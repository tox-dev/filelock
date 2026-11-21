from __future__ import annotations

import copy
import logging
import os
import subprocess  # ruff:ignore[suspicious-subprocess-import]  # interpreter exit needs a separate process
import sys
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from queue import Queue
from textwrap import dedent
from typing import TYPE_CHECKING, Final

import pytest

from filelock import AsyncStrictSoftFileLock, ReleasePendingError, StrictSoftFileLock, Timeout

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path

    from pytest_mock import MockerFixture

_WINDOWS: Final[pytest.MarkDecorator] = pytest.mark.skipif(sys.platform != "win32", reason="Windows release deadlines")


@pytest.fixture
def lock_path(tmp_path: Path) -> Path:
    return tmp_path / "strict.lock"


def test_async_strict_lock_rejects_sync_release_deadline(lock_path: Path) -> None:
    with pytest.raises(NotImplementedError, match="synchronous strict lock"):
        AsyncStrictSoftFileLock(lock_path).release_with_timeout(0)


@pytest.mark.requires_hard_links  # pragma: needs hard-link
@pytest.mark.parametrize(
    "timeout",
    [
        pytest.param(float("nan"), id="nan"),
        pytest.param(float("inf"), id="infinite"),
        pytest.param(-0.01, id="negative"),
    ],
)
def test_invalid_release_deadline_keeps_hold(lock_path: Path, timeout: float) -> None:
    with StrictSoftFileLock(lock_path) as lock:
        with pytest.raises(ValueError, match="timeout must be between"):
            lock.release_with_timeout(timeout)
        assert lock.is_locked


@pytest.mark.requires_hard_links  # pragma: needs hard-link
@pytest.mark.parametrize("thread_local", [pytest.param(True, id="thread-local"), pytest.param(False, id="shared")])
def test_timed_release_preserves_recursion(lock_path: Path, thread_local: bool) -> None:
    lock = StrictSoftFileLock(lock_path, thread_local=thread_local)
    lock.release_with_timeout(0)
    lock.acquire()
    lock.acquire()
    lock.release_with_timeout(0)
    assert (lock.lock_counter, len(lock.claims)) == (1, 2)
    lock.release_with_timeout(5)
    assert (lock.is_locked, lock.lock_counter, lock.claims) == (False, 0, ())


@_WINDOWS  # pragma: win32 cover
@pytest.mark.parametrize("thread_local", [pytest.param(True, id="thread-local"), pytest.param(False, id="shared")])
@pytest.mark.parametrize(
    "finish",
    [
        pytest.param(lambda lock: lock.release(), id="wait"),
        pytest.param(lambda lock: lock.release_with_timeout(5), id="retry"),
    ],
)
def test_pending_release_excludes_a_peer(
    lock_path: Path, mocker: MockerFixture, thread_local: bool, finish: Callable[[StrictSoftFileLock], None]
) -> None:
    lock = StrictSoftFileLock(lock_path, thread_local=thread_local)
    lock.acquire()
    entered, proceed = threading.Event(), threading.Event()
    release = lock._release

    def pause() -> None:
        entered.set()
        assert proceed.wait(10)
        release()

    mocker.patch.object(lock, "_release", autospec=True, side_effect=pause)
    peer = StrictSoftFileLock(lock_path)
    try:
        with pytest.raises(ReleasePendingError, match="still pending"):
            lock.release_with_timeout(0.03)
        assert entered.wait(5)
        with pytest.raises(ReleasePendingError):
            lock.release_with_timeout(0)
        with pytest.raises(Timeout):
            lock.acquire(blocking=False)
        assert (lock.is_locked, lock.lock_counter, len(peer.claims)) == (True, 1, 2)
        with pytest.raises(Timeout):
            peer.acquire(timeout=0)
    finally:
        proceed.set()
    # Shared release observers can hold this gate while the worker finishes.
    with lock._transition_lock:
        finish(lock)
    assert (lock.is_locked, lock.lock_counter, list(lock._claim_directory.glob("*.claim"))) == (False, 0, [])
    with peer.acquire(timeout=0):
        assert peer.is_locked


@_WINDOWS  # pragma: win32 cover
def test_pending_close_reports_the_original_error(lock_path: Path, mocker: MockerFixture) -> None:
    lock = StrictSoftFileLock(lock_path)
    lock.acquire()
    entered, proceed = threading.Event(), threading.Event()
    close = lock._close_released_fd
    failure = TimeoutError("filesystem close failed")

    def pause(fd: int, *, default_suppresses: bool) -> None:
        entered.set()
        assert proceed.wait(10)
        close(fd, default_suppresses=default_suppresses)
        raise failure

    mocker.patch.object(lock, "_close_released_fd", autospec=True, side_effect=pause)
    try:
        with pytest.raises(ReleasePendingError) as pending:
            lock.release_with_timeout(0.03)
        assert entered.wait(5)
        assert (lock.is_locked, list(lock._claim_directory.glob("*.claim"))) == (True, [])
        with StrictSoftFileLock(lock_path).acquire(timeout=-1, cancel_check=lambda: True) as peer:
            assert peer.is_locked
    finally:
        proceed.set()
    with pytest.raises(TimeoutError, match="filesystem close failed") as caught:
        lock.release()
    with pytest.raises(TimeoutError, match="filesystem close failed") as receipt_error:
        pending.value.wait()
    assert (caught.value is receipt_error.value is failure, lock.is_locked, lock.lock_counter) == (True, False, 0)


@_WINDOWS  # pragma: win32 cover
def test_failed_withdrawal_can_be_retried(lock_path: Path, mocker: MockerFixture) -> None:
    lock = StrictSoftFileLock(lock_path)
    lock.acquire()
    failure = PermissionError("could not withdraw claim")
    refused = mocker.patch.object(lock, "_release", side_effect=failure, autospec=True)
    with pytest.raises(PermissionError, match="could not withdraw") as caught:
        lock.release_with_timeout(5)
    assert (caught.value is failure, lock.is_locked, len(lock.claims)) == (True, True, 2)
    mocker.stop(refused)
    lock.release_with_timeout(5)
    assert (lock.is_locked, lock.claims) == (False, ())


@_WINDOWS  # pragma: win32 cover
def test_timed_release_respects_thread_ownership(lock_path: Path) -> None:
    lock = StrictSoftFileLock(lock_path)
    with lock, ThreadPoolExecutor(1) as pool:
        pool.submit(lock.release_with_timeout, 0).result(5)
        assert (len(lock.claims), lock.is_locked) == (2, True)


@_WINDOWS  # pragma: win32 cover
def test_force_release_observes_an_exited_owners_error(lock_path: Path, mocker: MockerFixture) -> None:
    lock = StrictSoftFileLock(lock_path)
    entered, proceed = threading.Event(), threading.Event()
    close = lock._close_released_fd

    def fail_close(fd: int, *, default_suppresses: bool) -> None:
        entered.set()
        assert proceed.wait(10)
        close(fd, default_suppresses=default_suppresses)
        msg = "delayed close error"
        raise OSError(msg)

    def own() -> None:
        lock.acquire()
        with pytest.raises(ReleasePendingError):
            lock.release_with_timeout(0)

    mocker.patch.object(lock, "_close_released_fd", autospec=True, side_effect=fail_close)
    try:
        with ThreadPoolExecutor(1) as pool:
            pool.submit(own).result(5)
        assert entered.wait(5)
    finally:
        proceed.set()
    with pytest.raises(OSError, match="delayed close error"):
        lock.release(force=True)
    lock.release(force=True)
    assert lock.claims == ()


@_WINDOWS  # pragma: win32 cover
def test_process_exit_does_not_join_a_pending_release(lock_path: Path) -> None:
    code = dedent("""
        import sys
        import threading
        from filelock import ReleasePendingError, StrictSoftFileLock

        class SlowRelease(StrictSoftFileLock):
            def _release(self):
                threading.Event().wait()

        lock = SlowRelease(sys.argv[1])
        lock.acquire()
        try:
            lock.release_with_timeout(0.01)
        except ReleasePendingError:
            print("pending")
    """)
    result = subprocess.run(
        [sys.executable, "-c", code, str(lock_path)], capture_output=True, text=True, timeout=10, check=True
    )
    assert result.stdout.strip() == "pending"
    lock = StrictSoftFileLock(lock_path)
    names = sorted(path.name for path in lock._claim_directory.glob("*.claim"))
    assert [name.split("-", 1)[0] for name in names] == ["held", "intent"]
    for name in names:
        lock.force_break(name)
    with lock.acquire(timeout=0):
        assert lock.is_locked


@_WINDOWS  # pragma: win32 cover
def test_worker_start_interruption_keeps_claim(lock_path: Path, mocker: MockerFixture) -> None:
    lock = StrictSoftFileLock(lock_path)
    lock.acquire()
    start = threading.Thread.start
    workers: list[threading.Thread] = []

    def interrupt(worker: threading.Thread) -> None:
        workers.append(worker)
        start(worker)
        with pytest.raises(RuntimeError, match="cannot acquire during this thread's release"):
            lock.acquire(timeout=-1)
        lock.release()
        assert (lock.is_locked, len(lock.claims)) == (True, 2)
        msg = "interrupted after startup"
        raise KeyboardInterrupt(msg)

    patched = mocker.patch.object(threading.Thread, "start", autospec=True, side_effect=interrupt)
    with pytest.raises(KeyboardInterrupt, match="after startup"):
        lock.release_with_timeout(5)
    (worker,) = workers
    worker.join(5)
    assert (worker.is_alive(), lock.is_locked, len(lock.claims)) == (False, True, 2)
    mocker.stop(patched)
    lock.release_with_timeout(5)
    assert lock.claims == ()


@_WINDOWS  # pragma: win32 cover
def test_busy_shared_gate_leaves_release_unstarted(lock_path: Path) -> None:
    lock = StrictSoftFileLock(lock_path, thread_local=False)
    with lock, ThreadPoolExecutor(1) as pool, lock._transition_lock:
        with pytest.raises(TimeoutError, match="waiting to start") as caught:
            pool.submit(lock.release_with_timeout, 0.02).result(5)
        assert (type(caught.value), lock.is_locked, len(lock.claims)) == (TimeoutError, True, 2)
    assert lock.claims == ()


@_WINDOWS  # pragma: win32 cover
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


@_WINDOWS  # pragma: win32 cover
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


@_WINDOWS  # pragma: win32 cover
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


@_WINDOWS  # pragma: win32 cover
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


@_WINDOWS  # pragma: win32 cover
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


@_WINDOWS  # pragma: win32 cover
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


@_WINDOWS  # pragma: win32 cover
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


@pytest.fixture  # pragma: win32 cover
def deferred_release(
    lock_path: Path, mocker: MockerFixture
) -> Iterator[tuple[StrictSoftFileLock, ReleasePendingError, threading.Event]]:
    lock = StrictSoftFileLock(lock_path)
    lock.acquire()
    proceed = threading.Event()
    release = lock._release

    def pause() -> None:
        assert proceed.wait(5)
        release()

    mocker.patch.object(lock, "_release", autospec=True, side_effect=pause)
    try:
        with pytest.raises(ReleasePendingError) as pending:
            lock.release_with_timeout(0)
        yield lock, pending.value, proceed
    finally:
        proceed.set()
        lock.release()


@_WINDOWS  # pragma: win32 cover
def test_pending_release_respects_the_acquires_deadline(
    deferred_release: tuple[StrictSoftFileLock, ReleasePendingError, threading.Event],
) -> None:
    lock, _, _ = deferred_release
    start = time.perf_counter()
    with pytest.raises(Timeout):
        lock.acquire(timeout=0.05)
    assert 0.05 <= time.perf_counter() - start < 2


@_WINDOWS  # pragma: win32 cover
@pytest.mark.parametrize(
    "contender",
    [
        pytest.param(lambda owner: owner, id="same"),
        pytest.param(lambda owner: StrictSoftFileLock(owner.lock_file), id="peer"),
    ],
)
def test_unbounded_acquire_outlasts_an_active_release(
    deferred_release: tuple[StrictSoftFileLock, ReleasePendingError, threading.Event],
    contender: Callable[[StrictSoftFileLock], StrictSoftFileLock],
) -> None:
    owner, pending, proceed = deferred_release

    def resume_owner() -> bool:
        proceed.set()
        return False

    with contender(owner).acquire(timeout=-1, cancel_check=resume_owner) as waiter:
        pending.wait(5)
        assert (waiter.is_locked, len(waiter.claims)) == (True, 2)


@_WINDOWS  # pragma: win32 cover
@pytest.mark.parametrize(
    "contender",
    [
        pytest.param(lambda owner: owner, id="same"),
        pytest.param(lambda owner: StrictSoftFileLock(owner.lock_file), id="peer"),
    ],
)
def test_pending_release_allows_unbounded_acquire_cancellation(
    deferred_release: tuple[StrictSoftFileLock, ReleasePendingError, threading.Event],
    contender: Callable[[StrictSoftFileLock], StrictSoftFileLock],
) -> None:
    lock, _, _ = deferred_release
    with pytest.raises(Timeout):
        contender(lock).acquire(timeout=-1, cancel_check=lambda: True)
    assert len(lock.claims) == 2


@_WINDOWS  # pragma: win32 cover
def test_timed_release_with_an_open_windows_reader(lock_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    assert sys.platform == "win32"
    from filelock import _windows

    lock = StrictSoftFileLock(lock_path)
    lock.acquire()
    held = next(lock._claim_directory.glob("held-*.claim"))
    proceed = threading.Event()

    def pause_before_withdrawal(record: logging.LogRecord) -> bool:
        if record.msg == "Attempting to release lock %s on %s" and record.args == (id(lock), str(lock_path)):
            assert proceed.wait(5)
        return True

    caplog.set_level(logging.DEBUG, logger="filelock")
    logger = logging.getLogger("filelock")
    with os.fdopen(_windows.open_read_without_oplock_wait(str(held)), "rb") as reader:
        logger.addFilter(pause_before_withdrawal)
        try:
            with pytest.raises(ReleasePendingError) as pending:
                lock.release_with_timeout(0.02)
            assert (held.exists(), lock.is_locked) == (True, True)
            with pytest.raises(Timeout):
                StrictSoftFileLock(lock_path).acquire(blocking=False)
        finally:
            proceed.set()
            logger.removeFilter(pause_before_withdrawal)
        pending.value.wait(5)
        assert not held.exists()
        with StrictSoftFileLock(lock_path, timeout=0) as peer:
            assert (peer.is_locked, bool(reader.read())) == (True, True)


@_WINDOWS  # pragma: win32 cover
def test_successor_acquire_does_not_consume_a_close_error(lock_path: Path, mocker: MockerFixture) -> None:
    lock = StrictSoftFileLock(lock_path, thread_local=False)
    lock.acquire()
    close = lock._close_released_fd
    failure = OSError("late close error")
    proceed = threading.Event()

    def fail_close(fd: int, *, default_suppresses: bool) -> None:
        assert proceed.wait(5)
        close(fd, default_suppresses=default_suppresses)
        raise failure

    patched = mocker.patch.object(lock, "_close_released_fd", autospec=True, side_effect=fail_close)
    try:
        with pytest.raises(ReleasePendingError) as pending:
            lock.release_with_timeout(0)
    finally:
        proceed.set()
    with lock.acquire(timeout=5):
        mocker.stop(patched)
        with pytest.raises(OSError, match="late close error") as caught:
            pending.value.wait()
        assert (caught.value is failure, lock.lock_counter, len(lock.claims)) == (True, 1, 2)
    assert lock.claims == ()


@_WINDOWS  # pragma: win32 cover
def test_failed_withdrawal_keeps_deadlock_detection(lock_path: Path, mocker: MockerFixture) -> None:
    owner = StrictSoftFileLock(lock_path)
    peer = StrictSoftFileLock(lock_path)
    owner.acquire()
    workers: Queue[threading.Thread] = Queue()
    proceed = threading.Event()

    def refuse() -> None:
        workers.put(threading.current_thread())
        assert proceed.wait(5)
        msg = "withdrawal refused"
        raise PermissionError(msg)

    patched = mocker.patch.object(owner, "_release", autospec=True, side_effect=refuse)
    with pytest.raises(ReleasePendingError) as pending:
        owner.release_with_timeout(0)
    worker = workers.get(timeout=5)

    def allow_withdrawal() -> bool:
        proceed.set()
        worker.join(5)
        assert not worker.is_alive()
        return False

    try:
        with pytest.raises(RuntimeError, match="Deadlock"):
            peer.acquire(timeout=-1, cancel_check=allow_withdrawal)
    finally:
        proceed.set()
    with pytest.raises(RuntimeError, match="release"):
        owner.acquire(timeout=0)
    with pytest.raises(Timeout):
        peer.acquire(timeout=0)
    with pytest.raises(PermissionError, match="withdrawal refused"):
        pending.value.wait(5)
    mocker.stop(patched)
    owner.release()
    with peer.acquire(timeout=0):
        assert peer.is_locked


@_WINDOWS  # pragma: win32 cover
def test_worker_can_enter_a_peer_only_after_withdrawal(lock_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    lock = StrictSoftFileLock(lock_path)
    peer = StrictSoftFileLock(lock_path, thread_local=False)
    lock.acquire()
    seen: list[str] = []

    def enter_peer(record: logging.LogRecord) -> bool:
        if record.args == (id(lock), str(lock_path)):
            if record.msg == "Attempting to release lock %s on %s":
                with pytest.raises(RuntimeError, match="Deadlock"):
                    peer.acquire(timeout=-1, cancel_check=lambda: True)
                seen.append("held")
            else:
                assert record.msg == "Lock %s released on %s"
                peer.acquire(timeout=-1, cancel_check=lambda: True)
                seen.append("withdrawn")
        return True

    caplog.set_level(logging.DEBUG, logger="filelock")
    logger = logging.getLogger("filelock")
    logger.addFilter(enter_peer)
    try:
        lock.release_with_timeout(5)
        assert (seen, peer.is_locked, len(peer.claims)) == (["held", "withdrawn"], True, 2)
    finally:
        logger.removeFilter(enter_peer)
        peer.release()
    assert peer.claims == ()


@_WINDOWS  # pragma: win32 cover
@pytest.mark.parametrize(
    "retry",
    [
        pytest.param(lambda lock: lock.release(), id="ordinary"),
        pytest.param(lambda lock: lock.release_with_timeout(0), id="timed"),
    ],
)
def test_retry_after_interrupted_release_commit_removes_the_registry_entry(
    lock_path: Path, mocker: MockerFixture, retry: Callable[[StrictSoftFileLock], None]
) -> None:
    lock = StrictSoftFileLock(lock_path)
    peer = StrictSoftFileLock(lock_path)
    lock.acquire()
    interrupted = mocker.patch.object(lock, "_commit_release", autospec=True, side_effect=KeyboardInterrupt("commit"))
    with pytest.raises(KeyboardInterrupt, match="commit"):
        lock.release()
    assert (lock.is_locked, lock.claims) == (False, ())
    with pytest.raises(RuntimeError, match="Deadlock"):
        peer.acquire(timeout=-1)
    mocker.stop(interrupted)
    retry(lock)
    with peer.acquire(timeout=-1):
        assert (peer.is_locked, lock.lock_counter) == (True, 0)


@_WINDOWS  # pragma: win32 cover
def test_acquisition_callback_cannot_start_a_release(lock_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    lock = StrictSoftFileLock(lock_path)
    claimed = threading.Event()

    def release_on_acquire(record: logging.LogRecord) -> bool:
        if record.msg == "Lock %s acquired on %s" and record.args == (id(lock), str(lock_path)):
            with pytest.raises(RuntimeError, match="during this thread's lock transition"):
                lock.release_with_timeout(5)
            claimed.set()
        return True

    caplog.set_level(logging.DEBUG, logger="filelock")
    logger = logging.getLogger("filelock")
    logger.addFilter(release_on_acquire)
    try:
        with lock:
            assert (claimed.is_set(), lock.is_locked, len(lock.claims)) == (True, True, 2)
    finally:
        logger.removeFilter(release_on_acquire)


@_WINDOWS  # pragma: win32 cover
def test_interrupted_release_wait_leaves_the_worker_pending(
    deferred_release: tuple[StrictSoftFileLock, ReleasePendingError, threading.Event], mocker: MockerFixture
) -> None:
    lock, receipt, proceed = deferred_release
    patched = mocker.patch.object(
        Future,
        "exception",
        autospec=True,
        side_effect=[KeyboardInterrupt("release wait interrupted"), KeyboardInterrupt("unexpected second wait")],
    )
    try:
        with pytest.raises(KeyboardInterrupt, match="release wait interrupted"):
            lock.release()
    finally:
        mocker.stop(patched)
    assert (proceed.is_set(), lock.is_locked, len(lock.claims)) == (False, True, 2)
    proceed.set()
    receipt.wait(5)
    assert (lock.is_locked, lock.claims) == (False, ())


@_WINDOWS  # pragma: win32 cover
@pytest.mark.parametrize("observers", [pytest.param("1", id="one-observer"), pytest.param("2", id="two-observers")])
def test_main_thread_receives_interrupt_while_waiting_for_release(lock_path: Path, observers: str) -> None:
    code = dedent("""
        import _thread
        import sys
        import threading
        from concurrent.futures import Future
        from filelock import ReleasePendingError, StrictSoftFileLock

        proceed = threading.Event()

        class SlowRelease(StrictSoftFileLock):
            def _release(self):
                assert proceed.wait(8)
                super()._release()

        lock = SlowRelease(sys.argv[1], thread_local=False)
        lock.acquire()
        try:
            lock.release_with_timeout(0)
        except ReleasePendingError as pending:
            receipt = pending

        def inside(thread, code):
            frame = sys._current_frames()[thread.ident]
            while frame:
                if frame.f_code is code:
                    return True
                frame = frame.f_back
            return False

        observers = []
        if sys.argv[2] == "2":
            observer = threading.Thread(target=lock.release)
            observers.append(observer)
            observer.start()
            for _ in range(300):
                if inside(observer, Future.exception.__code__):
                    break
                proceed.wait(0.01)
            else:
                raise AssertionError("second observer did not start waiting")

        def interrupt_wait():
            wait = lock.release.__code__ if observers else Future.exception.__code__
            while not proceed.wait(0.01):
                if inside(threading.main_thread(), wait):
                    _thread.interrupt_main()
                    return

        interrupt = threading.Timer(0.25, interrupt_wait)
        fallback = threading.Timer(3, proceed.set)
        fallback.start()
        interrupt.start()
        try:
            lock.release()
        except KeyboardInterrupt:
            assert not proceed.is_set()
            assert lock.is_locked and len(lock.claims) == 2
            print("interrupted while pending")
        finally:
            proceed.set()
            fallback.cancel()
            interrupt.join()
            fallback.join()
            for observer in observers:
                observer.join(5)
                assert not observer.is_alive()
        receipt.wait(5)
        assert not lock.is_locked and not lock.claims
    """)
    result = subprocess.run(
        [sys.executable, "-c", code, str(lock_path), observers], capture_output=True, text=True, timeout=15, check=True
    )
    assert result.stdout.strip() == "interrupted while pending"
