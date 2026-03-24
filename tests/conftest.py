from __future__ import annotations

import gc
import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from filelock._read_write import _cleanup_connections
else:
    try:
        from filelock._read_write import _cleanup_connections
    except ImportError:
        _cleanup_connections = None


def pytest_sessionfinish() -> None:
    if _cleanup_connections is not None:
        if hasattr(sys, "pypy_version_info"):
            gc.collect()
            gc.collect()
        _cleanup_connections()
        if hasattr(sys, "pypy_version_info"):
            gc.collect()
            gc.collect()
