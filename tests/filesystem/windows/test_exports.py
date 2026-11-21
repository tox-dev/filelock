from __future__ import annotations

import sys
from importlib import import_module

import pytest


@pytest.mark.skipif(sys.platform == "win32", reason="Windows exposes the native filesystem operations")
@pytest.mark.parametrize(
    "module",
    [
        pytest.param("filelock.internal.filesystem.windows", id="filesystem"),
        pytest.param("filelock.internal.filesystem.windows._bindings", id="bindings"),
        pytest.param("filelock.internal.filesystem.windows._directories", id="directories"),
        pytest.param("filelock.internal.filesystem.windows._handles", id="handles"),
        pytest.param("filelock.internal.filesystem.windows._oplocks", id="oplocks"),
        pytest.param("filelock.internal.soft.strict._windows", id="strict"),
    ],
)
def test_windows_modules_export_nothing_off_windows(module: str) -> None:  # pragma: win32 no cover
    assert import_module(module).__all__ == []
