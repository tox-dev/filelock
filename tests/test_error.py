from __future__ import annotations

import copy
import pickle  # ruff:ignore[suspicious-pickle-import]  # round-trips Timeout to assert it pickles
from typing import TYPE_CHECKING

import pytest

from filelock import SoftFileLockProtocolError, Timeout

if TYPE_CHECKING:
    from collections.abc import Callable


@pytest.mark.parametrize(
    ("extract", "expected"),
    [
        pytest.param(str, "The file lock '/path/to/lock' could not be acquired.", id="str"),
        pytest.param(repr, "Timeout('/path/to/lock')", id="repr"),
        pytest.param(lambda t: t.lock_file, "/path/to/lock", id="lock_file"),
        pytest.param(
            lambda t: t.__reduce__(), (Timeout, ("/path/to/lock",), {"_lock_file": "/path/to/lock"}), id="reduce"
        ),
    ],
)
def test_timeout_attribute(timeout: Timeout, extract: Callable[[Timeout], object], expected: object) -> None:
    assert extract(timeout) == expected


def test_timeout_pickle(timeout: Timeout) -> None:
    reloaded = pickle.loads(pickle.dumps(timeout))  # ruff:ignore[suspicious-pickle-usage]  # input is the Timeout built in this test
    assert (type(reloaded), str(reloaded), repr(reloaded), reloaded.lock_file) == (
        type(timeout),
        str(timeout),
        repr(timeout),
        timeout.lock_file,
    )


@pytest.fixture
def timeout() -> Timeout:
    return Timeout("/path/to/lock")


@pytest.mark.parametrize(
    "error",
    [Timeout("/path/to/lock"), SoftFileLockProtocolError("/path/to/lock", "claim", "invalid marker")],
)
@pytest.mark.parametrize(
    "clone",
    [copy.copy, copy.deepcopy, lambda error: pickle.loads(pickle.dumps(error))],  # ruff:ignore[suspicious-pickle-usage]  # only values created by this test
)
def test_exception_serialization_preserves_notes(error: OSError, clone: Callable[[OSError], OSError]) -> None:
    # Set the public notes attribute directly so the test also runs on Python 3.10.
    error.__notes__ = ["while acquiring the build cache"]
    error.__dict__["request_id"] = "request-1"
    restored = clone(error)
    assert restored.__dict__ == error.__dict__
    assert type(restored) is type(error)
    assert str(restored) == str(error)
