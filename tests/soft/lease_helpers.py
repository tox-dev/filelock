from __future__ import annotations

import time
from threading import Event
from typing import TYPE_CHECKING, Final, TypedDict

from filelock import (
    LeaseCompromise,
    SoftFileLease,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path


DURATION: Final[float] = 0.9
HEARTBEAT: Final[float] = 0.1
DEADLINE: Final[float] = 30


def make_lease(
    marker: Path,
    *,
    lease_duration: float = DURATION,
    timeout: float = 0.3,
    on_compromise: Callable[[LeaseCompromise], None] | None = None,
) -> SoftFileLease:
    return SoftFileLease(
        str(marker),
        timeout=timeout,
        lease_duration=lease_duration,
        heartbeat_interval=HEARTBEAT,
        on_compromise=on_compromise,
    )


class Reports:
    def __init__(self) -> None:
        self.seen: list[LeaseCompromise] = []
        self.landed: Final = Event()

    def __call__(self, compromise: LeaseCompromise) -> None:
        self.seen.append(compromise)
        self.landed.set()


def wait_until(condition: Callable[[], bool]) -> None:
    deadline: Final[float] = time.monotonic() + DURATION * 20
    while not condition() and time.monotonic() < deadline:
        time.sleep(HEARTBEAT / 2)  # simplint: ignore[sleep-in-test]  # needs a compromise signal


class LeaseSettings(TypedDict, total=False):
    lease_duration: float
    heartbeat_interval: float
    on_compromise: Callable[[LeaseCompromise], None]


__all__ = [
    "DEADLINE",
    "DURATION",
    "HEARTBEAT",
    "LeaseSettings",
    "Reports",
    "make_lease",
    "wait_until",
]
