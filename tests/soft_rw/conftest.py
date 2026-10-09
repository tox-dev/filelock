from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from unittest.mock import MagicMock

    from pytest_mock import MockerFixture


@pytest.fixture
def flushes(mocker: MockerFixture) -> MagicMock:
    # These tests commit by the hundred, and one flush can take a Windows runner most of a second.
    return mocker.patch("filelock._soft_rw._storage.os.fsync", autospec=True)
