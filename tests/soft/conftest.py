from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path

    from tests.soft.lease_helpers import LeaseSettings


@pytest.fixture
def marker(tmp_path: Path) -> Path:
    return tmp_path / "a.lock"


@pytest.fixture
def lease_settings() -> LeaseSettings:
    return {"lease_duration": 6, "heartbeat_interval": 1, "on_compromise": lambda _compromise: None}
