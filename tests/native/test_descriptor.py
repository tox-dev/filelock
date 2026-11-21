from __future__ import annotations

import os
from errno import EBADF
from typing import TYPE_CHECKING

import pytest
from capabilities import CAPABILITIES, _lists_every_open_descriptor

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


@pytest.mark.skipif(not CAPABILITIES["fd-directory"], reason="counting open descriptors needs a /dev/fd view")
def test_descriptors_on_counts_a_descriptor_numbered_above_the_static_nodes(  # pragma: needs fd-directory
    tmp_path: Path,
) -> None:
    with pytest.raises(OSError, match=rf"\[Errno {EBADF}\]"):  # dup2 would silently close whatever holds 100
        os.fstat(100)
    with (tmp_path / "held").open("w+b") as first:
        node = os.fstat(first.fileno())
        os.dup2(first.fileno(), 100)
        try:
            assert descriptors_on(node) == 2
        finally:
            os.close(100)


def test_complete_fd_directory_probe_rejects_a_static_view(tmp_path: Path) -> None:
    for number in range(64):
        (tmp_path / str(number)).touch()
    assert _lists_every_open_descriptor(str(tmp_path)) is False


def test_complete_fd_directory_probe_needs_a_view() -> None:
    assert _lists_every_open_descriptor(None) is False
