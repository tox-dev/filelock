from __future__ import annotations

import os
from errno import EACCES, EINVAL, ENOENT, ENOSPC
from typing import TYPE_CHECKING, Final

import pytest

from filelock._soft._strict._publication import _probe_link_follow_symlinks

if TYPE_CHECKING:
    from pytest_mock import MockerFixture


pytestmark: Final[pytest.MarkDecorator] = pytest.mark.requires_hard_links


@pytest.mark.parametrize(
    "error",
    [
        pytest.param(OSError(EINVAL, "unsupported option"), id="einval"),
        pytest.param(NotImplementedError("no follow_symlinks"), id="notimplemented"),
        pytest.param(ValueError("bad option"), id="valueerror"),
    ],
)
def test_strict_soft_link_follow_probe_reports_unhonored(mocker: MockerFixture, error: Exception) -> None:
    mocker.patch("os.link", side_effect=error, autospec=True)

    assert _probe_link_follow_symlinks() is False


@pytest.mark.parametrize(
    ("target", "error"),
    [
        pytest.param("tempfile.TemporaryDirectory", FileNotFoundError(ENOENT, "no temporary directory"), id="missing"),
        pytest.param("tempfile.TemporaryDirectory", OSError(EINVAL, "invalid temporary directory"), id="invalid-dir"),
        pytest.param("pathlib.Path.touch", OSError(ENOSPC, "no space left"), id="full"),
        pytest.param("pathlib.Path.touch", OSError(EINVAL, "invalid temporary file"), id="invalid-file"),
        pytest.param("os.link", PermissionError(EACCES, "denied"), id="denied"),
    ],
)
def test_strict_soft_link_follow_probe_preserves_runtime_support_on_environment_error(
    mocker: MockerFixture, target: str, error: OSError
) -> None:
    advertised = os.link in os.supports_follow_symlinks
    mocker.patch(target, side_effect=error, autospec=True)

    assert _probe_link_follow_symlinks() is advertised
