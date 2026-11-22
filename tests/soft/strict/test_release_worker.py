from __future__ import annotations

import subprocess  # ruff:ignore[suspicious-subprocess-import]  # interpreter exit needs a separate process
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
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


@WINDOWS_ONLY  # pragma: win32 cover
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


@WINDOWS_ONLY  # pragma: win32 cover
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


@WINDOWS_ONLY  # pragma: win32 cover
def test_timed_release_respects_thread_ownership(lock_path: Path) -> None:
    lock = StrictSoftFileLock(lock_path)
    with lock, ThreadPoolExecutor(1) as pool:
        pool.submit(lock.release_with_timeout, 0).result(5)
        assert (len(lock.claims), lock.is_locked) == (2, True)


@WINDOWS_ONLY  # pragma: win32 cover
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


@WINDOWS_ONLY  # pragma: win32 cover
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


@WINDOWS_ONLY  # pragma: win32 cover
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
