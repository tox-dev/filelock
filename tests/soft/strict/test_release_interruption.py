from __future__ import annotations

import logging
import subprocess  # ruff:ignore[suspicious-subprocess-import]  # interpreter exit needs a separate process
import sys
import threading
from concurrent.futures import Future
from queue import Queue
from textwrap import dedent
from typing import TYPE_CHECKING

import pytest

from filelock import ReleasePendingError, StrictSoftFileLock, Timeout

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from pytest_mock import MockerFixture
from .capability_marks import WINDOWS_ONLY


@WINDOWS_ONLY  # pragma: win32 cover
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


@WINDOWS_ONLY  # pragma: win32 cover
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


@WINDOWS_ONLY  # pragma: win32 cover
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


@WINDOWS_ONLY  # pragma: win32 cover
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


@WINDOWS_ONLY  # pragma: win32 cover
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


@WINDOWS_ONLY  # pragma: win32 cover
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


@WINDOWS_ONLY  # pragma: win32 cover
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
