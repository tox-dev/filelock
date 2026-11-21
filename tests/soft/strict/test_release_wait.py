from __future__ import annotations

import logging
import os
import sys
import threading
import time
from typing import TYPE_CHECKING

import pytest

from filelock import ReleasePendingError, StrictSoftFileLock, Timeout

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

from .capability_marks import WINDOWS_ONLY


@WINDOWS_ONLY  # pragma: win32 cover
def test_pending_release_respects_the_acquires_deadline(
    deferred_release: tuple[StrictSoftFileLock, ReleasePendingError, threading.Event],
) -> None:
    lock, _, _ = deferred_release
    start = time.perf_counter()
    with pytest.raises(Timeout):
        lock.acquire(timeout=0.05)
    assert 0.05 <= time.perf_counter() - start < 2


@WINDOWS_ONLY  # pragma: win32 cover
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


@WINDOWS_ONLY  # pragma: win32 cover
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


@WINDOWS_ONLY  # pragma: win32 cover
def test_timed_release_with_an_open_windows_reader(lock_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    assert sys.platform == "win32"
    import filelock._filesystem._windows._oplocks as oplocks

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
    with os.fdopen(oplocks.open_read_without_oplock_wait(str(held)), "rb") as reader:
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
