from __future__ import annotations

import os
from typing import TYPE_CHECKING

import pytest
from capabilities import CAPABILITIES, _lists_every_open_descriptor

from tests.descriptor_helpers import descriptors_on

if TYPE_CHECKING:
    from pathlib import Path

    from pytest_mock import MockerFixture


@pytest.mark.parametrize(
    ("has_view", "lists_open", "lists_closed", "expected"),
    [
        pytest.param(True, True, False, True, id="live-view"),
        pytest.param(True, True, True, False, id="static-nodes"),
        pytest.param(True, False, False, False, id="missing-probe"),
        pytest.param(False, False, False, False, id="no-view"),
    ],
)
def test_descriptor_directory_must_stop_listing_a_closed_probe(
    tmp_path: Path, mocker: MockerFixture, *, has_view: bool, lists_open: bool, lists_closed: bool, expected: bool
) -> None:
    with (tmp_path / "probe").open("w+b") as probe:
        path = mocker.patch("capabilities.Path", autospec=True).return_value
        path.is_dir.return_value = has_view
        path.open.return_value = probe
        path.exists.side_effect = lambda: lists_closed if probe.closed else lists_open

        assert _lists_every_open_descriptor() is expected


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
