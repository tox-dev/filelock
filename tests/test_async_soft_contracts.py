from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest

from filelock import AsyncSoftFileLease, AsyncStrictSoftFileLock, LeaseCompromise, Timeout

if TYPE_CHECKING:
    from pathlib import Path

_DURATION: float = 0.9
_HEARTBEAT: float = 0.1


@pytest.fixture
def marker(tmp_path: Path) -> Path:
    return tmp_path / "a.lock"


@pytest.mark.asyncio
async def test_async_strict_treats_a_foreign_marker_as_contention(marker: Path) -> None:
    await asyncio.to_thread(marker.write_text, "filelock/2\npid=999999\nhost=nowhere\nmode=strict\n", encoding="utf-8")
    lock = AsyncStrictSoftFileLock(str(marker), timeout=0.2)

    with pytest.raises(Timeout):
        await lock.acquire()
    assert await asyncio.to_thread(marker.exists)


@pytest.mark.asyncio
async def test_async_strict_publishes_its_owner(marker: Path) -> None:
    lock = AsyncStrictSoftFileLock(str(marker))

    async with lock:
        owner = lock.owner

    assert owner is not None
    assert owner.mode == "strict"


@pytest.mark.asyncio
async def test_async_lease_heartbeat_keeps_a_live_claim(marker: Path) -> None:
    lease = AsyncSoftFileLease(str(marker), lease_duration=_DURATION, heartbeat_interval=_HEARTBEAT)

    async with lease:
        await asyncio.sleep(_DURATION * 1.5)  # only a refreshing heartbeat keeps the claim past this
        assert lease.compromise is None
        assert lease.token is not None


@pytest.mark.asyncio
async def test_async_lease_reports_compromise_when_the_marker_vanishes(marker: Path) -> None:
    seen: list[LeaseCompromise] = []
    lease = AsyncSoftFileLease(
        str(marker), lease_duration=_DURATION, heartbeat_interval=_HEARTBEAT, on_compromise=seen.append
    )

    async with lease:
        await asyncio.to_thread(marker.unlink)
        await asyncio.sleep(_HEARTBEAT * 5)

    assert [c.reason for c in seen] == ["marker-missing"]
