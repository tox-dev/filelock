from __future__ import annotations

import itertools
import os
import sys
import time
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest

import filelock._soft._exclusive._publication  # simplint: ignore[private-import-path]  # tests internal behavior
from filelock._soft import MALFORMED_LOCK_AGE_THRESHOLD

if TYPE_CHECKING:
    from pathlib import Path

    from pytest_mock import MockerFixture, MockType


@pytest.fixture
def lock_path(tmp_path: Path) -> Path:
    return tmp_path / "test.lock"


@pytest.fixture
def soft_open_spy(mocker: MockerFixture) -> MockType:
    if sys.platform == "win32":  # pragma: win32 cover
        return mocker.spy(filelock._soft._exclusive._publication, "create_exclusive_non_reparse_fd")
    return mocker.spy(os, "open")  # pragma: win32 no cover


@pytest.fixture
def unchanged_past_grace(mocker: MockerFixture) -> None:
    # Each poll advances the observer's clock by one grace period.
    mocker.patch(
        "filelock._soft._exclusive._recovery.time",
        new=SimpleNamespace(
            monotonic=mocker.create_autospec(
                time.monotonic, side_effect=itertools.count(step=MALFORMED_LOCK_AGE_THRESHOLD)
            ),
            sleep=time.sleep,
        ),
    )
