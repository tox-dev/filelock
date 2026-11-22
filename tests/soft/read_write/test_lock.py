from __future__ import annotations

import threading
from contextlib import closing, nullcontext
from typing import TYPE_CHECKING, Final

import pytest

from filelock import AsyncSoftReadWriteLock, SoftReadWriteLock

from .lock_helpers import make_lock

if TYPE_CHECKING:
    from collections.abc import Callable
    from contextlib import AbstractContextManager
    from pathlib import Path


pytestmark: Final[list[pytest.MarkDecorator]] = [
    pytest.mark.requires_hard_links,
    pytest.mark.usefixtures("clear_singletons"),
]


@pytest.mark.parametrize(
    ("name", "value", "error", "problem"),
    [
        # Thread waits above TIMEOUT_MAX raise OverflowError after the lock is held.
        pytest.param(
            "heartbeat_interval",
            threading.TIMEOUT_MAX * 2,
            ValueError,
            "not exceed threading.TIMEOUT_MAX",
            id="heartbeat-max",
        ),
        pytest.param(
            "poll_interval", threading.TIMEOUT_MAX * 2, ValueError, "not exceed threading.TIMEOUT_MAX", id="poll-max"
        ),
        pytest.param("heartbeat_interval", "1", TypeError, "be a number of seconds, not str", id="heartbeat-str"),
        pytest.param("heartbeat_interval", True, TypeError, "be a number of seconds, not bool", id="heartbeat-bool"),
        pytest.param("stale_threshold", True, TypeError, "be a number of seconds, not bool", id="stale-bool"),
        pytest.param("poll_interval", True, TypeError, "be a number of seconds, not bool", id="poll-bool"),
    ],
)
def test_rejects_an_interval_it_cannot_use(
    tmp_path: Path, name: str, value: float, error: type[Exception], problem: str
) -> None:
    settings: Final[dict[str, float]] = {name: value}
    with pytest.raises(error, match=f"{name} must {problem}"):
        SoftReadWriteLock(
            tmp_path / "a.lock",
            is_singleton=False,
            heartbeat_interval=settings.get("heartbeat_interval", 30.0),
            stale_threshold=settings.get("stale_threshold", 90.0),
            poll_interval=settings.get("poll_interval", 0.25),
        )


@pytest.mark.parametrize(
    "lock_type",
    [pytest.param(SoftReadWriteLock, id="sync"), pytest.param(AsyncSoftReadWriteLock, id="async")],
)
@pytest.mark.parametrize(
    "keep",
    [
        pytest.param(lambda _path: nullcontext(), id="new"),
        pytest.param(lambda path: closing(SoftReadWriteLock(path)), id="cached"),
    ],
)
@pytest.mark.parametrize(
    ("settings", "message"),
    [
        pytest.param({name: value}, name, id=f"{name}-{label}")
        for name in ("heartbeat_interval", "stale_threshold", "poll_interval")
        for label, value in (("nan", float("nan")), ("infinity", float("inf")), ("negative-infinity", float("-inf")))
    ],
)
def test_rejects_invalid_intervals(
    tmp_path: Path,
    lock_type: type[SoftReadWriteLock | AsyncSoftReadWriteLock],
    keep: Callable[[Path], AbstractContextManager[SoftReadWriteLock | None]],
    settings: dict[str, float],
    message: str,
) -> None:
    path: Final[Path] = tmp_path / "timing.lock"
    # The singleton cache holds only weak references.
    with keep(path), pytest.raises(ValueError, match=rf"{message} must .*finite"):
        lock_type(
            path,
            heartbeat_interval=settings.get("heartbeat_interval", 30),
            stale_threshold=settings.get("stale_threshold"),
            poll_interval=settings.get("poll_interval", 0.25),
        )


@pytest.mark.parametrize(
    ("heartbeat_interval", "stale_threshold", "poll_interval", "match"),
    [
        pytest.param(0, None, 0.25, "heartbeat_interval must be positive", id="heartbeat-non-positive"),
        pytest.param(10, 5, 0.25, "stale_threshold must exceed", id="stale-not-greater"),
        pytest.param(30, None, 0, "poll_interval must be positive", id="poll-non-positive"),
        pytest.param(1, 3, 3, "poll_interval must be below", id="poll-not-below-stale"),
    ],
)
def test_rejects_invalid_interval_relationship(
    lock_file: str, heartbeat_interval: float, stale_threshold: float | None, poll_interval: float, match: str
) -> None:
    with pytest.raises(ValueError, match=match):
        SoftReadWriteLock(
            lock_file,
            is_singleton=False,
            heartbeat_interval=heartbeat_interval,
            stale_threshold=stale_threshold,
            poll_interval=poll_interval,
        )


def test_public_attributes(lock_file: str) -> None:
    lock = SoftReadWriteLock(
        lock_file,
        timeout=5,
        blocking=False,
        heartbeat_interval=10,
        stale_threshold=45,
        poll_interval=0.5,
        is_singleton=False,
    )
    try:
        assert (lock.lock_file, lock.timeout) == (lock_file, 5)
        assert lock.blocking is False
        assert (lock.heartbeat_interval, lock.stale_threshold, lock.poll_interval) == (10, 45, pytest.approx(0.5))
    finally:
        lock.close()


def test_default_stale_threshold_is_triple_heartbeat(lock_file: str) -> None:
    lock = SoftReadWriteLock(lock_file, heartbeat_interval=12, is_singleton=False)
    try:
        assert lock.stale_threshold == 36
    finally:
        lock.close()


def test_lock_file_in_a_missing_directory_is_created(tmp_path: Path) -> None:
    lock = make_lock(str(tmp_path / "nested" / "deeper" / "x.lock"))
    try:
        with lock.write_lock(timeout=1):
            assert (lock.generation, (tmp_path / "nested" / "deeper").is_dir()) == (1, True)
    finally:
        lock.close()
