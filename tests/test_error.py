from __future__ import annotations

import pickle  # noqa: S403

from filelock import Timeout


def test_timeout_str() -> None:
    timeout = Timeout("/path/to/lock")
    assert str(timeout) == "The file lock '/path/to/lock' could not be acquired."


def test_timeout_repr() -> None:
    timeout = Timeout("/path/to/lock")
    assert repr(timeout) == "Timeout('/path/to/lock')"


def test_timeout_lock_file() -> None:
    timeout = Timeout("/path/to/lock")
    assert timeout.lock_file == "/path/to/lock"


def test_timeout_pickle() -> None:
    timeout = Timeout("/path/to/lock")
    timeout_loaded = pickle.loads(pickle.dumps(timeout))  # noqa: S301

    assert timeout.__class__ == timeout_loaded.__class__
    assert str(timeout) == str(timeout_loaded)
    assert repr(timeout) == repr(timeout_loaded)
    assert timeout.lock_file == timeout_loaded.lock_file
