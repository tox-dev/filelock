from __future__ import annotations

import gc
import os
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from capabilities import CAPABILITIES

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from pytest_mock import MockerFixture

# Coverage restarts in every patched subprocess and re-imports the plugin there; pytest's pythonpath reaches only
# this process, so export it.
os.environ["PYTHONPATH"] = os.pathsep.join(
    part for part in (str(Path(__file__).parent.parent / "tasks"), os.environ.get("PYTHONPATH")) if part
)


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    # StrictSoftFileLock publishes claims with hard links, so tests marked requires_hard_links cannot run where
    # os.link is missing (Termux/Android CPython ships without it). The no-hard-link degradation itself is covered by
    # the os.link tests in test_filelock.py, which carry no marker and run everywhere.
    if not hasattr(os, "link"):  # pragma: no cover  # the body runs only on Termux/Android, absent from the CI matrix
        skip = pytest.mark.skip(reason="StrictSoftFileLock requires os.link (hard links), absent on Termux/Android")
        for item in items:
            if item.get_closest_marker("requires_hard_links") is not None:
                item.add_marker(skip)


@pytest.fixture(autouse=True, scope="session")
def _tracemalloc_lookup_without_support() -> Iterator[None]:
    # pytest's thread and unraisable exception hooks ask tracemalloc where the offending object was allocated. Where
    # that lookup raises (GraalPy), the hook reports "Failed to process thread exception" in place of the thread's real
    # error, so hand it the answer tracemalloc gives while it is not tracing.
    with pytest.MonkeyPatch.context() as patch:
        if not CAPABILITIES["tracemalloc-object-traceback"]:  # pragma: lacks tracemalloc-object-traceback
            patch.setattr("tracemalloc.get_object_traceback", lambda _source: None)
        yield


@pytest.fixture(autouse=True)
def _collect_garbage() -> Iterator[None]:
    # Multiprocessing objects register finalizers that close pipes, unlink semaphores and write to the resource
    # tracker; when cyclic garbage from one test is collected during a later one, those run against whatever that
    # later test has patched onto os.close or os.write. Collect after every test so finalizers fire in the test that
    # created the objects, before any such patch is in place. This runs after mocker's undo, since autouse fixtures
    # set up first and tear down last.
    yield
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
        mocker.patch("filelock._api.os.close", side_effect=close)

    def close(fd: int) -> None:
        # filelock._api.os is the os module, so this patches os.close process-wide. Raise only for the descriptor
        # under test. An unrelated close inside a GC finalizer would escape as an unraisable exception.
        if fd == locked_fd:
            raise release_error
        real_close(fd)  # pragma: no cover  # only an unrelated close (e.g. a GC finalizer) reaches here

    yield capture, release_error, release_cause
    if locked_fd is not None:  # pragma: no branch  # every consumer calls capture, so this is always set
        real_close(locked_fd)
