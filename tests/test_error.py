from __future__ import annotations

import copy
import pickle  # ruff:ignore[suspicious-pickle-import]  # round-trips exceptions created in these tests
from functools import partial
from typing import TYPE_CHECKING, Final

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
    ],
)
def test_timeout_attribute(extract: Callable[[Timeout], str], expected: str) -> None:
    assert extract(Timeout("/path/to/lock")) == expected


def test_exception_serialization_preserves_diagnostics(
    error: Timeout | SoftFileLockProtocolError,
    clone: Callable[[Timeout | SoftFileLockProtocolError], Timeout | SoftFileLockProtocolError],
) -> None:
    # Python 3.10 supports the notes attribute but has no add_note().
    error.__notes__ = ["while acquiring the build cache"]
    vars(error)["request_id"] = "request-1"
    restored: Final = clone(error)
    assert (
        type(restored),
        str(restored),
        repr(restored),
        restored.args,
        restored.lock_file,
        restored.__notes__,
        vars(restored)["request_id"],
    ) == (
        type(error),
        str(error),
        repr(error),
        error.args,
        error.lock_file,
        ["while acquiring the build cache"],
        "request-1",
    )


def test_exception_serialization_creates_distinct_instance(
    error: Timeout | SoftFileLockProtocolError,
    clone: Callable[[Timeout | SoftFileLockProtocolError], Timeout | SoftFileLockProtocolError],
) -> None:
    assert clone(error) is not error


def test_exception_serialization_preserves_note_alias(
    error: Timeout | SoftFileLockProtocolError,
    clone: Callable[[Timeout | SoftFileLockProtocolError], Timeout | SoftFileLockProtocolError],
) -> None:
    error.__notes__ = ["original"]
    vars(error)["diagnostics"] = error.__notes__
    restored: Final = clone(error)
    assert vars(restored)["diagnostics"] is restored.__notes__


def test_exception_copy_shares_notes(error: Timeout | SoftFileLockProtocolError) -> None:
    error.__notes__ = ["original"]
    copy.copy(error).__notes__.append("copy")
    assert error.__notes__ == ["original", "copy"]


def test_exception_deepcopy_isolates_notes(error: Timeout | SoftFileLockProtocolError) -> None:
    error.__notes__ = ["original"]
    copy.deepcopy(error).__notes__.append("copy")
    assert error.__notes__ == ["original"]


def test_exception_serialization_preserves_cycle(
    error: Timeout | SoftFileLockProtocolError,
    clone: Callable[[Timeout | SoftFileLockProtocolError], Timeout | SoftFileLockProtocolError],
) -> None:
    vars(error)["related"] = error
    restored: Final = clone(error)
    assert vars(restored)["related"] is (error if clone is copy.copy else restored)


def test_protocol_error_serialization_preserves_claim(
    clone: Callable[[Timeout | SoftFileLockProtocolError], Timeout | SoftFileLockProtocolError],
) -> None:
    restored: Final = clone(SoftFileLockProtocolError("/lock", "claim", "invalid marker"))
    assert isinstance(restored, SoftFileLockProtocolError)
    assert (restored.claim_name, restored.reason) == ("claim", "invalid marker")


@pytest.fixture(
    params=[pytest.param("timeout", id="timeout"), pytest.param("claim", id="claim"), pytest.param(None, id="no-claim")]
)
def error(request: pytest.FixtureRequest) -> Timeout | SoftFileLockProtocolError:
    if request.param == "timeout":
        return Timeout("/path/to/lock")
    return SoftFileLockProtocolError("/path/to/lock", request.param, "invalid marker")


def _pickle_round_trip(
    error: Timeout | SoftFileLockProtocolError, *, protocol: int
) -> Timeout | SoftFileLockProtocolError:
    return pickle.loads(pickle.dumps(error, protocol=protocol))  # ruff:ignore[suspicious-pickle-usage]  # input is created by these tests


@pytest.fixture(
    params=[pytest.param(copy.copy, id="copy"), pytest.param(copy.deepcopy, id="deepcopy")]
    + [
        pytest.param(partial(_pickle_round_trip, protocol=p), id=f"pickle-{p}")
        for p in range(pickle.HIGHEST_PROTOCOL + 1)
    ],
)
def clone(
    request: pytest.FixtureRequest,
) -> Callable[[Timeout | SoftFileLockProtocolError], Timeout | SoftFileLockProtocolError]:
    return request.param
