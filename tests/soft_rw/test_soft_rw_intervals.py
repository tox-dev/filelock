from __future__ import annotations

import sys
from typing import TYPE_CHECKING, Final

import pytest

from filelock import AsyncSoftReadWriteLock, SoftReadWriteLock

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.parametrize(
    "lock_type",
    [pytest.param(SoftReadWriteLock, id="sync"), pytest.param(AsyncSoftReadWriteLock, id="async")],
)
@pytest.mark.parametrize("cached", [pytest.param(False, id="new"), pytest.param(True, id="cached")])
@pytest.mark.parametrize(
    ("settings", "message"),
    [
        pytest.param({name: value}, name, id=f"{name}-{label}")
        for name in ("heartbeat_interval", "stale_threshold", "poll_interval")
        for label, value in (("nan", float("nan")), ("infinity", float("inf")), ("negative-infinity", float("-inf")))
    ]
    + [
        pytest.param({"heartbeat_interval": sys.float_info.max}, "stale_threshold", id="default-overflow"),
    ],
)
def test_rejects_invalid_intervals(
    tmp_path: Path,
    lock_type: type[SoftReadWriteLock | AsyncSoftReadWriteLock],
    cached: bool,
    settings: dict[str, float],
    message: str,
) -> None:
    path: Final = tmp_path / "timing.lock"
    # Keep the weakly cached instance alive through the second construction.
    existing: Final = SoftReadWriteLock(path) if cached else None
    try:
        with pytest.raises(ValueError, match=rf"{message} must .*finite"):
            lock_type(
                path,
                heartbeat_interval=settings.get("heartbeat_interval", 30),
                stale_threshold=settings.get("stale_threshold"),
                poll_interval=settings.get("poll_interval", 0.25),
            )
    finally:
        if existing is not None:
            existing.close()
