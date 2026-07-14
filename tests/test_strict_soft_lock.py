from __future__ import annotations

import os
import socket
from typing import TYPE_CHECKING

import pytest

from filelock import SoftFileLock, StrictSoftFileLock, Timeout

if TYPE_CHECKING:
    from pathlib import Path

_ANCIENT: float = 0.0


@pytest.fixture
def marker(tmp_path: Path) -> Path:
    return tmp_path / "a.lock"


def _publish(marker: Path, content: str, *, age: float | None = None) -> None:
    marker.write_text(content, encoding="utf-8")
    if age is not None:
        os.utime(marker, (age, age))


@pytest.mark.parametrize(
    ("record", "case"),
    [
        pytest.param(
            "filelock/2\npid=999999\nhost=nowhere\nmode=strict\n", "owner-on-another-host", id="owner-unknown"
        ),
        pytest.param("not a marker at all\n", "malformed", id="malformed"),
        pytest.param("4242\nsomehost\n", "legacy protocol 1", id="legacy"),
        pytest.param("", "empty", id="empty"),
    ],
)
def test_strict_treats_unreadable_marker_as_contention(marker: Path, record: str, case: str) -> None:
    # A strict lock never reclaims: every one of these markers is old enough that SoftFileLock would evict it.
    _publish(marker, record, age=_ANCIENT)
    lock = StrictSoftFileLock(str(marker), timeout=0.2)

    with pytest.raises(Timeout):
        lock.acquire()
    assert marker.exists(), f"strict acquisition removed a {case} marker"


def test_strict_does_not_reclaim_a_dead_owner(marker: Path) -> None:
    # SoftFileLock evicts a same-host marker whose PID is gone; a strict lock refuses to guess the holder died.
    _publish(marker, f"filelock/2\npid=999999\nhost={socket.gethostname()}\nmode=strict\n")
    lock = StrictSoftFileLock(str(marker), timeout=0.2)

    with pytest.raises(Timeout):
        lock.acquire()
    assert marker.exists()


def test_strict_does_not_reclaim_by_age(marker: Path) -> None:
    _publish(marker, f"filelock/2\npid={os.getpid()}\nhost={socket.gethostname()}\nmode=strict\n", age=_ANCIENT)
    lock = StrictSoftFileLock(str(marker), timeout=0.2)

    with pytest.raises(Timeout):
        lock.acquire()


def test_strict_publishes_its_owner(marker: Path) -> None:
    lock = StrictSoftFileLock(str(marker))

    with lock:
        owner = lock.owner

    assert owner is not None
    assert (owner.pid, owner.hostname, owner.mode, owner.token) == (
        os.getpid(),
        socket.gethostname(),
        "strict",
        None,
    )


def test_strict_reports_the_holding_process(marker: Path) -> None:
    lock = StrictSoftFileLock(str(marker))

    with lock:
        assert (lock.pid, lock.is_lock_held_by_us) == (os.getpid(), True)


def test_strict_reports_no_owner_without_a_marker(marker: Path) -> None:
    lock = StrictSoftFileLock(str(marker))

    assert (lock.owner, lock.pid, lock.is_lock_held_by_us) == (None, None, False)


def test_strict_force_break_lets_a_contender_in(marker: Path) -> None:
    _publish(marker, "filelock/2\npid=999999\nhost=nowhere\nmode=strict\n")
    lock = StrictSoftFileLock(str(marker), timeout=0.2)
    with pytest.raises(Timeout):
        lock.acquire()

    lock.force_break()

    with lock:
        assert lock.is_lock_held_by_us


def test_strict_drops_lifetime_with_a_warning(marker: Path) -> None:
    with pytest.warns(UserWarning, match="a strict lock never reclaims a marker by age"):
        lock = StrictSoftFileLock(str(marker), lifetime=5)

    assert lock.lifetime is None


def test_soft_lock_evicts_a_strict_marker(marker: Path) -> None:
    # Protocol 1 and protocol 2 do not exclude each other: a legacy contender reads the strict record as malformed and
    # evicts it once past the malformed grace period. Documented in how-to; pinned here so the loss stays visible.
    _publish(marker, "filelock/2\npid=999999\nhost=nowhere\nmode=strict\n", age=_ANCIENT)
    legacy = SoftFileLock(str(marker), timeout=2)

    with legacy:
        assert legacy.is_lock_held_by_us

    assert not marker.exists()
