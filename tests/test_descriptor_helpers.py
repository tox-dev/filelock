from __future__ import annotations

import os
from typing import TYPE_CHECKING

import pytest
from capabilities import CAPABILITIES

from tests.descriptor_helpers import descriptors_on

if TYPE_CHECKING:
    from pathlib import Path

    from pytest_mock import MockerFixture


@pytest.mark.skipif(not CAPABILITIES["fd-directory"], reason="counting open descriptors needs a /dev/fd view")
def test_descriptors_on_probes_every_number_behind_an_incomplete_directory(  # pragma: needs fd-directory
    tmp_path: Path, mocker: MockerFixture
) -> None:
    mocker.patch.dict(CAPABILITIES, {"complete-fd-directory": False})
    with (tmp_path / "held").open("w+b") as first:
        node = os.fstat(first.fileno())
        second = os.dup(first.fileno())
        mocker.patch.object(os, "sysconf", autospec=True, return_value=second + 1)
        try:
            assert descriptors_on(node) == 2
        finally:
            os.close(second)
        assert descriptors_on(node) == 1
    assert descriptors_on(node) == 0
