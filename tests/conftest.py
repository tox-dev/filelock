from __future__ import annotations

import gc
import sys

try:
    from filelock._read_write import _cleanup_connections
except ImportError:
    _cleanup_connections = None  # type: ignore[assignment, misc]


def pytest_sessionfinish() -> None:
    if _cleanup_connections is not None:
        if hasattr(sys, "pypy_version_info"):
            gc.collect()
            gc.collect()
        _cleanup_connections()
        if hasattr(sys, "pypy_version_info"):
            gc.collect()
            gc.collect()
