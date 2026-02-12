from __future__ import annotations

from filelock._read_write import _cleanup_connections


def pytest_sessionfinish() -> None:
    _cleanup_connections()
