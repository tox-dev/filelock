from __future__ import annotations

import math
import os
from typing import TYPE_CHECKING, Final

import pytest

from tests.read_write_helpers import writer_gets_in

pytest.importorskip("sqlite3")
from pathlib import Path

from filelock import ReadWriteLock, SoftReadWriteLock
from filelock._sqlite._transactions import _timeout_for_sqlite

if TYPE_CHECKING:
    from pytest_mock import MockerFixture


pytestmark: Final[pytest.MarkDecorator] = pytest.mark.usefixtures("clear_sqlite_singletons")


@pytest.mark.parametrize(
    ("timeout", "blocking", "already_waited", "milliseconds"),
    [
        pytest.param(10.0, False, 0.0, 0, id="nonblocking"),
        pytest.param(-1.0, True, 0.0, 1_999_999_999, id="infinite"),
        pytest.param(5.0, True, 2.0, 3_000, id="remaining"),
        pytest.param(1.0, True, 2.0, 0, id="expired"),
        pytest.param(0.0, True, 0.0, 0, id="zero"),
        pytest.param(3_000_000.0, True, 0.0, 1_999_999_999, id="clamped"),
    ],
)
def test_timeout_for_sqlite(timeout: float, blocking: bool, already_waited: float, milliseconds: int) -> None:
    assert _timeout_for_sqlite(timeout, blocking=blocking, already_waited=already_waited) == milliseconds


def test_timeout_for_sqlite_negative_timeout_raises() -> None:
    with pytest.raises(ValueError, match="timeout must be a non-negative number or -1"):
        _timeout_for_sqlite(-2, blocking=True, already_waited=0.0)


def test_singleton_returns_same_instance(lock_file: str) -> None:
    lock1 = ReadWriteLock(lock_file)
    lock2 = ReadWriteLock(lock_file)
    assert lock1 is lock2


def test_singleton_different_files(tmp_path: Path) -> None:
    lock1 = ReadWriteLock(str(tmp_path / "a.db"))
    lock2 = ReadWriteLock(str(tmp_path / "b.db"))
    assert lock1 is not lock2


def test_singleton_rejects_directory_change_during_construction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = tmp_path / "state"
    spool = tmp_path / "spool"
    state.mkdir()
    spool.mkdir()

    class MovingPath:
        calls = 0

        def __fspath__(self) -> str:
            self.calls += 1
            if self.calls == 2:
                monkeypatch.chdir(spool)
            return "gate.db"

    monkeypatch.chdir(state)
    with pytest.raises(RuntimeError, match="path changed during singleton construction"):
        ReadWriteLock(MovingPath())

    with ReadWriteLock(state / "gate.db").write_lock():
        assert not writer_gets_in(str(state / "gate.db"))


def test_singleton_rejects_different_timeout(lock_file: str) -> None:
    lock = ReadWriteLock(lock_file, timeout=1.0)
    with pytest.raises(ValueError, match="Singleton lock created with"):
        ReadWriteLock(lock_file, timeout=2.0)
    del lock


@pytest.mark.parametrize(
    ("first", "second"), [pytest.param(-1, math.inf, id="inf"), pytest.param(math.nan, math.nan, id="nan")]
)
def test_singleton_accepts_a_timeout_that_waits_the_same(
    lock_type: type[ReadWriteLock | SoftReadWriteLock], first: float, second: float, lock_file: str
) -> None:
    assert lock_type(lock_file, timeout=first) is lock_type(lock_file, timeout=second)


@pytest.mark.parametrize("timeout", [pytest.param(True, id="bool"), pytest.param("1", id="str")])
def test_constructor_rejects_a_timeout_that_is_not_a_number(
    lock_type: type[ReadWriteLock | SoftReadWriteLock], timeout: bool | str, lock_file: str
) -> None:
    with pytest.raises(TypeError, match="timeout must be a number of seconds"):
        lock_type(
            lock_file,
            timeout=timeout,  # ty: ignore[invalid-argument-type]  # the rejected type is the contract
            is_singleton=False,
        )


@pytest.mark.parametrize("blocking", [pytest.param("false", id="str"), pytest.param(0, id="int")])
def test_constructor_rejects_a_blocking_that_is_not_a_bool(
    lock_type: type[ReadWriteLock | SoftReadWriteLock], blocking: str | int, lock_file: str
) -> None:
    with pytest.raises(TypeError, match="blocking must be a bool"):
        lock_type(
            lock_file,
            blocking=blocking,  # ty: ignore[invalid-argument-type]  # the rejected type is the contract
            is_singleton=False,
        )


def test_singleton_rejects_different_blocking(lock_file: str) -> None:
    lock = ReadWriteLock(lock_file, blocking=True)
    with pytest.raises(ValueError, match="Singleton lock created with"):
        ReadWriteLock(lock_file, blocking=False)
    del lock


def test_non_singleton_creates_new_instance(lock_file: str) -> None:
    lock1 = ReadWriteLock(lock_file, is_singleton=False)
    lock2 = ReadWriteLock(lock_file, is_singleton=False)
    assert lock1 is not lock2


def test_init_sets_attributes(lock_file: str) -> None:
    lock = ReadWriteLock(lock_file, timeout=5.0, blocking=False, is_singleton=False)
    assert (lock.lock_file, lock.timeout) == (lock_file, pytest.approx(5.0))
    assert lock.blocking is False
    assert lock._hold.level == 0
    assert lock._hold.mode is None
    assert lock._hold.write_thread_id is None
    assert Path(lock_file).is_file()


def test_write_lock_context_manager_overrides_defaults(lock_file: str) -> None:
    lock = ReadWriteLock(lock_file, timeout=10.0, blocking=False, is_singleton=False)
    with lock.write_lock(timeout=5.0, blocking=True):
        assert lock._hold.mode == "write"


def test_get_lock_returns_the_singleton(lock_file: str) -> None:
    lock = ReadWriteLock.get_lock(lock_file)
    assert isinstance(lock, ReadWriteLock)
    assert lock is ReadWriteLock(lock_file)


def test_meta_resets_class_state_after_pid_change(lock_file: str) -> None:
    class _Sub(ReadWriteLock):
        pass

    _Sub._instances_pid = -1
    lock = _Sub(lock_file)
    try:
        assert isinstance(lock, _Sub)
        assert _Sub._instances_pid == os.getpid()
    finally:
        lock.close()


def test_meta_raises_when_pid_changes_during_singleton_construction(lock_file: str, mocker: MockerFixture) -> None:
    class _Sub(ReadWriteLock):
        pass

    pid_box = [os.getpid()]
    mocker.patch("filelock._sqlite._fork.GETPID", side_effect=lambda: pid_box[0], autospec=True)
    original_init = _Sub.__init__

    def _init_then_fork(
        self: _Sub,
        lock_file: str | os.PathLike[str],
        timeout: float = -1,
        *,
        blocking: bool = True,
        is_singleton: bool = True,
    ) -> None:
        original_init(self, lock_file, timeout, blocking=blocking, is_singleton=is_singleton)
        pid_box[0] += 1

    mocker.patch.object(_Sub, "__init__", _init_then_fork)

    with pytest.raises(RuntimeError, match="construction cannot continue after fork"):
        _Sub(lock_file)
