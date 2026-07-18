from __future__ import annotations

import os
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from pytest_mock import MockerFixture


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    # StrictSoftFileLock publishes claims with hard links, so tests marked requires_hard_links cannot run where
    # os.link is missing (Termux/Android CPython ships without it). The no-hard-link degradation itself is covered by
    # the os.link tests in test_filelock.py, which carry no marker and run everywhere.
    if hasattr(os, "link"):
        return
    skip = pytest.mark.skip(reason="StrictSoftFileLock requires os.link (hard links), absent on Termux/Android")
    for item in items:
        if item.get_closest_marker("requires_hard_links") is not None:
            item.add_marker(skip)


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
        mocker.patch("filelock._api.os.close", side_effect=close)

    def close(fd: int) -> None:
        # filelock._api.os is the os module, so this patches os.close process-wide. Raise only for the descriptor
        # under test. An unrelated close inside a GC finalizer would escape as an unraisable exception.
        if fd == locked_fd:
            raise release_error
        real_close(fd)

    yield capture, release_error, release_cause
    if locked_fd is not None:
        real_close(locked_fd)
