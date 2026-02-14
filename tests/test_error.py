from __future__ import annotations

import pickle  # noqa: S403

from filelock import FileLockDeadlockError, Timeout


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


def test_deadlock_error_str() -> None:
    error = FileLockDeadlockError("/path/to/lock")
    assert "would deadlock" in str(error)
    assert "/path/to/lock" in str(error)
    assert "is_singleton=True" in str(error)


def test_deadlock_error_repr() -> None:
    error = FileLockDeadlockError("/path/to/lock")
    assert repr(error) == "FileLockDeadlockError('/path/to/lock')"


def test_deadlock_error_lock_file() -> None:
    error = FileLockDeadlockError("/path/to/lock")
    assert error.lock_file == "/path/to/lock"


def test_deadlock_error_pickle() -> None:
    error = FileLockDeadlockError("/path/to/lock")
    error_loaded = pickle.loads(pickle.dumps(error))  # noqa: S301

    assert error.__class__ == error_loaded.__class__
    assert str(error) == str(error_loaded)
    assert repr(error) == repr(error_loaded)
    assert error.lock_file == error_loaded.lock_file
