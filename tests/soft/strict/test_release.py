from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from filelock import AsyncStrictSoftFileLock, StrictSoftFileLock

if TYPE_CHECKING:
    from pathlib import Path


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
