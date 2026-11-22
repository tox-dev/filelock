from __future__ import annotations

import signal
from pkgutil import resolve_name
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
    from types import FrameType

    from pytest_mock import MockerFixture


@NEEDS_PROMPT_SIGNAL_DELIVERY  # pragma: needs prompt-signal-delivery
@pytest.mark.parametrize("thread_local", [pytest.param(True, id="thread_local"), pytest.param(False, id="shared")])
@pytest.mark.parametrize(
    ("timeout", "blocking", "expected"),
    [
        pytest.param(-1, True, RuntimeError, id="indefinite"),
        pytest.param(1, True, Timeout, id="finite"),
        pytest.param(-1, False, Timeout, id="nonblocking"),
    ],
)
def test_handler_acquiring_peer_during_poll_detects_deadlock(
    tmp_path: Path, mocker: MockerFixture, thread_local: bool, timeout: float, blocking: bool, expected: type[Exception]
) -> None:
    path = str(tmp_path / "a")
    outer = SoftFileLock(path, thread_local=thread_local, is_singleton=False)
    peer = SoftFileLock(path, thread_local=thread_local, is_singleton=False)
    original = outer._acquire_with_fork_tracking
    pending = [signal.SIGINT]

    def signal_then_acquire() -> None:
        signals = pending.copy()
        pending.clear()
        list(map(signal.raise_signal, signals))
        original()

    def handoff(_signum: int, _frame: FrameType | None) -> None:
        peer.acquire(blocking=False)

    mocker.patch.object(outer, "_acquire_with_fork_tracking", side_effect=signal_then_acquire, autospec=True)
    previous = signal.signal(signal.SIGINT, handoff)
    try:
        with pytest.raises(expected):
            outer.acquire(timeout=timeout, blocking=blocking, cancel_check=lambda: True)
        assert (outer.is_locked, peer.is_locked) == (False, True)
        with pytest.raises(RuntimeError, match="already held by a different"):
            SoftFileLock(path, is_singleton=False).acquire(cancel_check=lambda: True)
    finally:
        signal.signal(signal.SIGINT, previous)
        peer.release()
        outer.release()


@NEEDS_PROMPT_SIGNAL_DELIVERY  # pragma: needs prompt-signal-delivery
def test_acquire_from_a_signal_handler_during_an_acquire_fails_fast(
    tmp_path: Path, mocker: MockerFixture, soft_create_path: str
) -> None:
    lock: Final[SoftFileLock] = SoftFileLock(str(tmp_path / "a"))

    def acquire_in_handler(_signum: int, _frame: FrameType | None) -> None:
        lock.acquire()

    mocker.patch(
        soft_create_path,
        autospec=True,
        side_effect=lambda *_args, **_kwargs: signal.raise_signal(signal.SIGINT),
    )
    previous = signal.signal(signal.SIGINT, acquire_in_handler)
    try:
        with pytest.raises(RuntimeError, match="acquired again while this thread's acquire of it waits"):
            lock.acquire()
    finally:
        signal.signal(signal.SIGINT, previous)
    mocker.stopall()

    lock.acquire(blocking=False)
    lock.release()
    assert not (tmp_path / "a").exists()


@NEEDS_PROMPT_SIGNAL_DELIVERY  # pragma: needs prompt-signal-delivery
@pytest.mark.parametrize("thread_local", [pytest.param(True, id="thread_local"), pytest.param(False, id="shared")])
@pytest.mark.parametrize(
    "interrupted",
    [pytest.param("fcntl.flock", id="after_unlock", marks=NEEDS_FCNTL), pytest.param("os.close", id="after_close")],
)
def test_acquire_from_a_signal_handler_during_a_release_is_refused(
    tmp_path: Path, mocker: MockerFixture, thread_local: bool, interrupted: str
) -> None:
    lock: Final[SoftFileLock] = FileLock(str(tmp_path / "a"), thread_local=thread_local)
    lock.acquire()

    def acquire_in_handler(_signum: int, _frame: FrameType | None) -> None:
        lock.acquire()

    # Reentry after unlock can observe a hold the OS has already dropped.
    real = resolve_name(interrupted)
    pending: Final[list[signal.Signals]] = [signal.SIGINT]

    def call_then_signal_once(*args: int) -> None:
        real(*args)
        # Only the first call raises the signal so the retried release can finish.
        signals = pending.copy()
        pending.clear()
        list(map(signal.raise_signal, signals))

    mocker.patch(interrupted, autospec=True, side_effect=call_then_signal_once)
    previous = signal.signal(signal.SIGINT, acquire_in_handler)
    try:
        with pytest.raises(RuntimeError, match="acquired again while this thread's release of it runs"):
            lock.release()
    finally:
        signal.signal(signal.SIGINT, previous)
    mocker.stopall()

    with FileLock(str(tmp_path / "a"), blocking=False) as peer:
        assert peer.is_locked
