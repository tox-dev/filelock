from __future__ import annotations

import os
import socket
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING, Final

import pytest

from filelock import SoftFileLock, Timeout

if TYPE_CHECKING:
    from collections.abc import Generator

    from pytest_mock import MockerFixture


@pytest.mark.parametrize(
    "mode", [pytest.param(0o444, id="acl"), pytest.param(0o464, id="group"), pytest.param(0o446, id="other")]
)
@pytest.mark.parametrize("effective_ids", [pytest.param(True, id="supported"), pytest.param(False, id="unsupported")])
def test_writability_uses_open_credentials(
    readonly_marker: Path, mocker: MockerFixture, mode: int, *, effective_ids: bool
) -> None:
    readonly_marker.chmod(mode)

    def access(filename: str, permission: int, *, effective_ids: bool = False) -> bool:
        assert (filename, permission) == (str(readonly_marker), os.W_OK)
        return effective_ids == supported

    supported: Final = effective_ids
    probe: Final = mocker.patch("os.access", autospec=True, side_effect=access)
    mocker.patch("os.supports_effective_ids", {probe} if effective_ids else set())
    with pytest.raises(Timeout):
        SoftFileLock(readonly_marker).acquire(timeout=0)


def test_writability_allows_creation_after_marker_disappears(readonly_marker: Path, mocker: MockerFixture) -> None:
    def access(filename: str, permission: int, *, effective_ids: bool = False) -> bool:
        assert (permission, effective_ids) == (os.W_OK, os.access in os.supports_effective_ids)
        readonly_marker.chmod(0o644)
        Path(filename).unlink()
        return False

    mocker.patch("os.access", autospec=True, side_effect=access)
    with (lock := SoftFileLock(readonly_marker)).acquire(timeout=0):
        assert lock.is_locked


def test_writability_rejects_denied_marker(readonly_marker: Path, mocker: MockerFixture) -> None:
    mocker.patch("os.access", autospec=True, return_value=False)
    with pytest.raises(PermissionError):
        SoftFileLock(readonly_marker).acquire(timeout=0)


@pytest.fixture
def readonly_marker(tmp_path: Path) -> Generator[Path]:
    path: Final = tmp_path / "test.lock"
    path.write_text(f"424242\n{socket.gethostname()}-other\n", encoding="utf-8")
    path.chmod(0o444)
    try:
        yield path
    finally:
        with suppress(FileNotFoundError):
            path.chmod(0o644)
