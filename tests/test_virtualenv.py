from __future__ import annotations

import sys
from typing import TYPE_CHECKING

import pytest
from virtualenv import cli_run

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.skipif(
    hasattr(sys, "pypy_version_info"),
    reason="PyPy segfaults in SQLite Cursor.__del__._reset when GC runs during virtualenv imports",
)
def test_virtualenv(tmp_path: Path) -> None:
    cli_run([str(tmp_path), "--no-pip", "--no-setuptools", "--no-periodic-update"])
