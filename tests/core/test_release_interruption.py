from __future__ import annotations

import os
import signal
import sys
import threading
from errno import EIO
from typing import TYPE_CHECKING, Final

import pytest

from filelock import (
    FileLock,
    SoftFileLock,
    Timeout,
)
from tests.capability_marks import (
    NEEDS_FCNTL,
    NEEDS_PROMPT_SIGNAL_DELIVERY,
)

if TYPE_CHECKING:
    from pathlib import Path

    from pytest_mock import MockerFixture


@NEEDS_FCNTL  # pragma: needs fcntl
def test_release_reentered_after_unlock_closes_descriptor_once(tmp_path: Path, mocker: MockerFixture) -> None:
    # typeshed exposes fcntl only on POSIX.
    assert sys.platform != "win32"
    import fcntl

    lock: Final[SoftFileLock] = FileLock(str(tmp_path / "a.lock"))
    lock.acquire()
    real_flock = fcntl.flock
    unlocked: Final[list[int]] = []

    def unlock_then_reenter(fd: int, operation: int) -> None:
        real_flock(fd, operation)
        unlocked.append(fd)
        # A signal handler landing right after the kernel unlock runs this on the releasing thread.
        lock.release(force=True)

    mocker.patch("fcntl.flock", autospec=True, side_effect=unlock_then_reenter)
    close = mocker.spy(os, "close")
    lock.release()
    assert ([call.args for call in close.call_args_list], lock.is_locked) == ([(unlocked[0],)], False)


def test_reentered_release_keeps_the_outer_hold_until_unlock(tmp_path: Path, mocker: MockerFixture) -> None:
    lock_path = str(tmp_path / "a.lock")
    lock = FileLock(lock_path, is_singleton=False)
    peer = FileLock(lock_path, is_singleton=False)
    lock.acquire()
    pending = [None]
    observed: list[bool] = []

    def release_after_reentry(*_args: object) -> None:
        if pending:
            pending.pop()
            lock.release(force=True)
            observed.append(lock.is_locked)
            with pytest.raises(Timeout):
                peer.acquire(blocking=False)

    mocker.patch("filelock._core._release._LOGGER.debug", autospec=True, side_effect=release_after_reentry)
    lock.release()
    assert (observed, lock.is_locked) == ([True], False)
    with peer:
        assert peer.is_locked


def test_interrupted_release_keeps_a_new_threads_hold(tmp_path: Path, mocker: MockerFixture) -> None:
    lock_path = str(tmp_path / "a.lock")
    lock = FileLock(lock_path, thread_local=False, is_singleton=False)
    lock.acquire()
    start, attempting, entered, finish = (threading.Event() for _ in range(4))

    def hold_after_release() -> None:
        start.wait()
        attempting.set()
        with lock:
            entered.set()
            finish.wait()

    worker = threading.Thread(target=hold_after_release, daemon=True)
    worker.start()
    release_current = lock._release_current
    pending = [None]

    def release_then_interrupt(*, force: bool) -> None:
        release_current(force=force)
        if pending:
            pending.pop()
            start.set()
            assert attempting.wait(1)
            assert not entered.wait(0.1)
            raise KeyboardInterrupt

    mocker.patch.object(lock, "_release_current", side_effect=release_then_interrupt, autospec=True)
    try:
        with pytest.raises(KeyboardInterrupt):
            lock.release()
        assert entered.wait(1)
        with pytest.raises(Timeout):
            FileLock(lock_path, is_singleton=False).acquire(blocking=False)
        assert lock.is_locked
    finally:
        finish.set()
        worker.join(2)
        lock.release(force=True)


def test_interrupted_release_while_entering_the_gate_retries(
    tmp_path: Path, mocker: MockerFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    lock_path = str(tmp_path / "a.lock")
    lock = FileLock(lock_path, thread_local=False, is_singleton=False)
    lock.acquire()
    transition = lock._transition_lock
    gate = mocker.create_autospec(transition, instance=True)
    pending = [None]

    def enter_gate() -> bool:
        if pending:
            pending.pop()
            raise KeyboardInterrupt
        return transition.acquire()

    gate.__enter__.side_effect = enter_gate
    gate.__exit__.side_effect = lambda *_args: transition.release()
    monkeypatch.setattr(lock, "_transition_lock", gate)
    with pytest.raises(KeyboardInterrupt):
        lock.release()
    assert not lock.is_locked
    with FileLock(lock_path, blocking=False, is_singleton=False):
        pass


def test_interrupted_release_retries_after_another_thread_nests(tmp_path: Path, mocker: MockerFixture) -> None:
    lock_path = str(tmp_path / "a.lock")
    lock = FileLock(lock_path, thread_local=False, is_singleton=False)
    lock.acquire()
    lock.acquire()
    start, attempting, entered, finish = (threading.Event() for _ in range(4))
    release_current = lock._release_current
    pending = [None]

    def nest() -> None:
        start.wait()
        attempting.set()
        with lock:
            entered.set()
            finish.wait()

    worker = threading.Thread(target=nest, daemon=True)
    worker.start()

    def interrupt_after_nested_hold(*, force: bool) -> None:
        release_current(force=force)
        if pending:
            pending.pop()
            start.set()
            assert attempting.wait(1)
            assert not entered.wait(0.1)
            raise KeyboardInterrupt

    mocker.patch.object(lock, "_release_current", side_effect=interrupt_after_nested_hold, autospec=True)
    try:
        with pytest.raises(KeyboardInterrupt):
            lock.release()
        assert entered.wait(1)
        finish.set()
        worker.join(2)
        assert lock.is_locked
        with pytest.raises(Timeout):
            FileLock(lock_path, is_singleton=False).acquire(blocking=False)
    finally:
        finish.set()
        worker.join(2)
        lock.release(force=True)


@NEEDS_FCNTL  # pragma: needs fcntl
def test_errno_after_unix_unlock_refuses_reentrant_acquire(tmp_path: Path, mocker: MockerFixture) -> None:
    assert sys.platform != "win32"
    import fcntl

    lock_path = tmp_path / "a.lock"
    lock = FileLock(lock_path, timeout=0)
    lock.acquire()
    real_flock = fcntl.flock
    interrupted = False

    def unlock_then_raise(fd: int, operation: int) -> None:
        nonlocal interrupted
        real_flock(fd, operation)
        if operation == fcntl.LOCK_UN and not interrupted:
            interrupted = True
            raise OSError(EIO, "signal handler")

    mocker.patch("fcntl.flock", autospec=True, side_effect=unlock_then_raise)
    with pytest.raises(OSError, match="signal handler"):
        lock.release()
    with FileLock(lock_path, timeout=0), pytest.raises(RuntimeError, match=r"call release\(\) again"):
        lock.acquire()
    lock.release()
    with lock:
        assert lock.is_locked


@NEEDS_FCNTL  # pragma: needs fcntl
def test_errno_before_unix_unlock_keeps_explicit_retry(tmp_path: Path, mocker: MockerFixture) -> None:
    assert sys.platform != "win32"
    import fcntl

    lock_path = tmp_path / "a.lock"
    lock = FileLock(lock_path, timeout=0)
    lock.acquire()
    lock.acquire()
    real_flock = fcntl.flock
    interrupted = False

    def raise_then_unlock(fd: int, operation: int) -> None:
        nonlocal interrupted
        if operation == fcntl.LOCK_UN and not interrupted:
            interrupted = True
            raise OSError(EIO, "unlock failed")
        real_flock(fd, operation)

    mocker.patch("fcntl.flock", autospec=True, side_effect=raise_then_unlock)
    with pytest.raises(OSError, match="unlock failed"):
        lock.release(force=True)
    with pytest.raises(Timeout):
        FileLock(lock_path, timeout=0).acquire()
    with pytest.raises(RuntimeError, match=r"call release\(\) again"):
        lock.acquire()
    lock.release()
    with FileLock(lock_path, timeout=0):
        assert not lock.is_locked


@NEEDS_PROMPT_SIGNAL_DELIVERY  # pragma: needs prompt-signal-delivery
@pytest.mark.parametrize("thread_local", [pytest.param(True, id="thread_local"), pytest.param(False, id="shared")])
def test_release_interrupted_by_a_signal_before_the_backend_completes(
    tmp_path: Path, mocker: MockerFixture, thread_local: bool
) -> None:
    lock: Final[SoftFileLock] = FileLock(str(tmp_path / "a"), thread_local=thread_local)
    lock.acquire()
    pending: Final[list[signal.Signals]] = [signal.SIGINT]

    def signal_once(*_args: object) -> None:
        signals = pending.copy()
        pending.clear()
        list(map(signal.raise_signal, signals))

    mocker.patch("filelock._core._release._LOGGER.debug", autospec=True, side_effect=signal_once)
    previous = signal.signal(signal.SIGINT, signal.default_int_handler)
    try:
        with pytest.raises(KeyboardInterrupt):
            lock.release()
    finally:
        signal.signal(signal.SIGINT, previous)
    mocker.stopall()

    assert not lock.is_locked
    with FileLock(str(tmp_path / "a"), blocking=False) as peer:
        assert peer.is_locked
