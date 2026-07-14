from __future__ import annotations

import gc
import os
import sys
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from pytest_mock import MockerFixture

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


@pytest.fixture
def close_failure(
    mocker: MockerFixture,
) -> Iterator[tuple[Callable[[int], None], OSError, RuntimeError]]:
    locked_fd: int | None = None
    release_error = OSError("release failed")
    release_cause = RuntimeError("release cause")
    release_error.__cause__ = release_cause
    release_error.__suppress_context__ = True
    real_close = os.close

    def capture(fd: int) -> None:
        nonlocal locked_fd
        locked_fd = fd
        mocker.patch("filelock._api.os.close", side_effect=release_error)

    yield capture, release_error, release_cause
    if locked_fd is not None:
        real_close(locked_fd)
