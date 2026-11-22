from __future__ import annotations

import pickle  # ruff:ignore[suspicious-pickle-import]  # round-trips values defined by this package
from types import FunctionType

import pytest

import filelock


@pytest.mark.parametrize(
    "name",
    [name for name in filelock.__all__ if isinstance(getattr(filelock, name), (type, FunctionType))],
)
def test_public_global_pickles_by_reference(name: str) -> None:
    value = getattr(filelock, name)
    payload = pickle.dumps(value)
    assert pickle.loads(payload) is value  # ruff:ignore[suspicious-pickle-usage]  # payload made here
