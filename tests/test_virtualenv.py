from __future__ import annotations

from typing import TYPE_CHECKING

from virtualenv import cli_run  # type: ignore[import-untyped]

if TYPE_CHECKING:
    from pathlib import Path


def test_virtualenv(tmp_path: Path) -> None:
    cli_run([str(tmp_path), "--no-pip", "--no-setuptools", "--no-periodic-update"])
