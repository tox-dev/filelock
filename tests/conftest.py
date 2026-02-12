from __future__ import annotations

try:
    from filelock._read_write import _cleanup_connections
except ModuleNotFoundError:
    _cleanup_connections = None  # type: ignore[assignment, misc]


def pytest_sessionfinish() -> None:
    if _cleanup_connections is not None:
        _cleanup_connections()
