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
    if _cleanup_connections is None:
        return
    # PyPy runs finalizers during GC rather than on refcount drop, so force lock handles closed around cleanup.
    on_pypy = hasattr(sys, "pypy_version_info")
    if on_pypy:
        gc.collect()
        gc.collect()
    _cleanup_connections()
    if on_pypy:
        gc.collect()
        gc.collect()
