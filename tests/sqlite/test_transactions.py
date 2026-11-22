from __future__ import annotations

import threading
from typing import TYPE_CHECKING, Final, Literal

import pytest

from tests.read_write_helpers import writer_gets_in

pytest.importorskip("sqlite3")
import sqlite3

from filelock import AcquireReturnProxy, ReadWriteLock, Timeout

if TYPE_CHECKING:
    from pytest_mock import MockerFixture


pytestmark: Final[pytest.MarkDecorator] = pytest.mark.usefixtures("clear_sqlite_singletons")


def test_acquire_release_read(lock_file: str) -> None:
    lock = ReadWriteLock(lock_file, is_singleton=False)
    proxy = lock.acquire_read()
    assert (lock._hold.level, lock._hold.mode) == (1, "read")
    lock.release()
    assert lock._hold.level == 0
    assert lock._hold.mode is None
    assert isinstance(proxy, AcquireReturnProxy)


def test_acquire_release_write(lock_file: str) -> None:
    lock = ReadWriteLock(lock_file, is_singleton=False)
    lock.acquire_write()
    assert (lock._hold.level, lock._hold.mode, lock._hold.write_thread_id) == (1, "write", threading.get_ident())
    lock.release()
    assert lock._hold.level == 0
    assert lock._hold.mode is None
    assert lock._hold.write_thread_id is None


@pytest.mark.parametrize(
    "mode",
    [
        pytest.param("read", id="read"),
        pytest.param("write", id="write"),
    ],
)
def test_reentrant_lock(lock_file: str, mode: Literal["read", "write"]) -> None:
    lock = ReadWriteLock(lock_file, is_singleton=False)
    acquire = getattr(lock, f"acquire_{mode}")
    acquire()
    acquire()
    assert lock._hold.level == 2
    lock.release()
    assert (lock._hold.level, lock._hold.mode) == (1, mode)
    lock.release()
    assert lock._hold.level == 0


def test_upgrade_read_to_write_prohibited(lock_file: str) -> None:
    lock = ReadWriteLock(lock_file, is_singleton=False)
    lock.acquire_read()
    with pytest.raises(RuntimeError, match=r"already holding a read lock.*upgrade not allowed"):
        lock.acquire_write()
    lock.release()


def test_downgrade_write_to_read_prohibited(lock_file: str) -> None:
    lock = ReadWriteLock(lock_file, is_singleton=False)
    lock.acquire_write()
    with pytest.raises(RuntimeError, match=r"already holding a write lock.*downgrade not allowed"):
        lock.acquire_read()
    lock.release()


def test_release_unheld_lock_raises(lock_file: str) -> None:
    lock = ReadWriteLock(lock_file, is_singleton=False)
    with pytest.raises(RuntimeError, match="not held"):
        lock.release()


def test_release_force_unheld_is_noop(lock_file: str) -> None:
    lock = ReadWriteLock(lock_file, is_singleton=False)
    lock.release(force=True)
    assert writer_gets_in(lock_file)
    lock.close()


def test_release_force_resets_level(lock_file: str) -> None:
    lock = ReadWriteLock(lock_file, is_singleton=False)
    lock.acquire_write()
    lock.acquire_write()
    assert lock._hold.level == 2
    lock.release(force=True)
    assert lock._hold.level == 0
    assert lock._hold.mode is None


def test_release_force_closes_lingering_connection_at_zero_level(lock_file: str) -> None:
    lock = ReadWriteLock(lock_file, is_singleton=False)
    lock.acquire_write()
    lock._hold.level = 0
    lock.release(force=True)
    assert lock._con is None
    assert lock._hold.level == 0


@pytest.mark.parametrize(
    "mode",
    [
        pytest.param("read", id="read"),
        pytest.param("write", id="write"),
    ],
)
def test_lock_context_manager(lock_file: str, mode: Literal["read", "write"]) -> None:
    lock = ReadWriteLock(lock_file, is_singleton=False)
    with getattr(lock, f"{mode}_lock")():
        assert (lock._hold.level, lock._hold.mode) == (1, mode)
    assert lock._hold.level == 0
    assert lock._hold.mode is None


@pytest.mark.parametrize(
    "mode",
    [
        pytest.param("read", id="read"),
        pytest.param("write", id="write"),
    ],
)
def test_lock_uses_instance_defaults(lock_file: str, mode: Literal["read", "write"]) -> None:
    lock = ReadWriteLock(lock_file, timeout=3.0, blocking=True, is_singleton=False)
    with getattr(lock, f"{mode}_lock")():
        assert lock._hold.mode == mode


def test_read_lock_custom_timeout(lock_file: str) -> None:
    lock = ReadWriteLock(lock_file, is_singleton=False)
    with lock.read_lock(timeout=5.0):
        assert lock._hold.mode == "read"


def test_write_lock_custom_blocking(lock_file: str) -> None:
    lock = ReadWriteLock(lock_file, is_singleton=False)
    with lock.write_lock(blocking=True):
        assert lock._hold.mode == "write"


def test_read_lock_context_manager_overrides_defaults(lock_file: str) -> None:
    lock = ReadWriteLock(lock_file, timeout=10.0, blocking=False, is_singleton=False)
    with lock.read_lock(timeout=5.0, blocking=True):
        assert lock._hold.mode == "read"


def test_close_releases_lock_and_closes_connection(lock_file: str) -> None:
    lock = ReadWriteLock(lock_file, is_singleton=False)
    lock.acquire_write()
    assert lock._hold.level == 1
    lock.close()
    assert lock._hold.level == 0
    with pytest.raises(sqlite3.ProgrammingError, match="Cannot operate on a closed database"):
        lock.acquire_read()


def test_close_on_unheld_lock_closes_connection(lock_file: str) -> None:
    lock = ReadWriteLock(lock_file, is_singleton=False)
    lock.close()
    with pytest.raises(sqlite3.ProgrammingError, match="Cannot operate on a closed database"):
        lock.acquire_read()


def test_write_lock_reentrant_from_different_thread_prohibited(lock_file: str) -> None:
    lock = ReadWriteLock(lock_file, is_singleton=False)
    lock.acquire_write()
    error: RuntimeError | None = None

    def try_reenter() -> None:
        nonlocal error
        try:
            lock.acquire_write()
        except RuntimeError as exc:
            error = exc

    thread = threading.Thread(target=try_reenter)
    thread.start()
    thread.join(timeout=5)
    lock.release()
    assert error is not None
    assert "while it is held by thread" in str(error)


@pytest.mark.parametrize(
    ("first", "second"),
    [
        pytest.param("read", "write", id="read-then-write"),
        pytest.param("write", "read", id="write-then-read"),
    ],
)
def test_sequential_mode_switch(lock_file: str, first: str, second: str) -> None:
    lock = ReadWriteLock(lock_file, is_singleton=False)
    with getattr(lock, f"{first}_lock")():
        assert not writer_gets_in(lock_file)
    with getattr(lock, f"{second}_lock")():
        assert not writer_gets_in(lock_file)


def test_non_blocking_read_fails_when_write_held(lock_file: str) -> None:
    holder_lock = ReadWriteLock(lock_file, is_singleton=False)
    contender_lock = ReadWriteLock(lock_file, is_singleton=False)
    acquired = threading.Event()

    def hold_write() -> None:
        with holder_lock.write_lock():
            acquired.set()
            threading.Event().wait(timeout=2)

    thread = threading.Thread(target=hold_write)
    thread.start()
    acquired.wait(timeout=5)

    with pytest.raises(Timeout):
        contender_lock.acquire_read(blocking=False)

    thread.join(timeout=5)


def test_non_blocking_write_fails_when_read_held(lock_file: str) -> None:
    holder_lock = ReadWriteLock(lock_file, is_singleton=False)
    contender_lock = ReadWriteLock(lock_file, is_singleton=False)
    acquired = threading.Event()

    def hold_read() -> None:
        with holder_lock.read_lock():
            acquired.set()
            threading.Event().wait(timeout=2)

    thread = threading.Thread(target=hold_read)
    thread.start()
    acquired.wait(timeout=5)

    with pytest.raises(Timeout):
        contender_lock.acquire_write(blocking=False)

    thread.join(timeout=5)


def test_timeout_read_expires(lock_file: str) -> None:
    holder_lock = ReadWriteLock(lock_file, is_singleton=False)
    contender_lock = ReadWriteLock(lock_file, is_singleton=False)
    acquired = threading.Event()

    def hold_write() -> None:
        with holder_lock.write_lock():
            acquired.set()
            threading.Event().wait(timeout=3)

    thread = threading.Thread(target=hold_write)
    thread.start()
    acquired.wait(timeout=5)

    with pytest.raises(Timeout):
        contender_lock.acquire_read(timeout=0.2)

    thread.join(timeout=5)


def test_nested_read_context_managers(lock_file: str) -> None:
    lock = ReadWriteLock(lock_file, is_singleton=False)
    with lock.read_lock():
        assert lock._hold.level == 1
        with lock.read_lock():
            assert lock._hold.level == 2
            with lock.read_lock():
                assert lock._hold.level == 3
            assert lock._hold.level == 2
        assert lock._hold.level == 1
    assert lock._hold.level == 0


def test_nested_write_context_managers(lock_file: str) -> None:
    lock = ReadWriteLock(lock_file, is_singleton=False)
    with lock.write_lock():
        assert lock._hold.level == 1
        with lock.write_lock():
            assert lock._hold.level == 2
        assert lock._hold.level == 1
    assert lock._hold.level == 0


@pytest.mark.parametrize(
    "mode",
    [
        pytest.param("read", id="read"),
        pytest.param("write", id="write"),
    ],
)
def test_non_blocking_transaction_lock_timeout(lock_file: str, mode: Literal["read", "write"]) -> None:
    lock = ReadWriteLock(lock_file, is_singleton=False)
    lock._transaction_lock.acquire()
    try:
        with pytest.raises(Timeout):
            getattr(lock, f"acquire_{mode}")(blocking=False)
    finally:
        lock._transaction_lock.release()


@pytest.mark.parametrize(
    "mode",
    [
        pytest.param("read", id="read"),
        pytest.param("write", id="write"),
    ],
)
def test_non_blocking_with_timeout_no_value_error(lock_file: str, mode: Literal["read", "write"]) -> None:
    lock = ReadWriteLock(lock_file, is_singleton=False)
    lock._transaction_lock.acquire()
    try:
        with pytest.raises(Timeout):
            getattr(lock, f"acquire_{mode}")(blocking=False, timeout=5.0)
    finally:
        lock._transaction_lock.release()


@pytest.mark.parametrize(
    "mode",
    [
        pytest.param("read", id="read"),
        pytest.param("write", id="write"),
    ],
)
def test_finite_timeout_transaction_lock(lock_file: str, mode: Literal["read", "write"]) -> None:
    lock = ReadWriteLock(lock_file, is_singleton=False)
    lock._transaction_lock.acquire()
    try:
        with pytest.raises(Timeout):
            getattr(lock, f"acquire_{mode}")(timeout=0.1)
    finally:
        lock._transaction_lock.release()


def test_double_check_read_reentrance(lock_file: str, mocker: MockerFixture) -> None:
    lock = ReadWriteLock(lock_file, is_singleton=False)
    real_lock = lock._transaction_lock
    mock_lock = mocker.create_autospec(real_lock)

    def fake_acquire(blocking: bool = True, timeout: float = -1) -> bool:
        lock._hold.level = 1
        lock._hold.mode = "read"
        _fake_open_transaction(lock, mocker)
        return real_lock.acquire(blocking, timeout)

    mock_lock.acquire = fake_acquire
    mock_lock.release = real_lock.release
    lock._transaction_lock = mock_lock
    proxy = lock.acquire_read()
    assert lock._hold.level == 2
    assert proxy is not None
    lock._hold.level = 0
    lock._hold.mode = None
    lock._owned_connection.connection, lock._owned_connection.transaction_released = None, True
    lock.release(force=True)


def _fake_open_transaction(lock: ReadWriteLock, mocker: MockerFixture) -> None:
    # A level stands only while its transaction does, so a faked level needs a transaction behind it too.
    lock._owned_connection.connection = mocker.create_autospec(sqlite3.Connection, instance=True, in_transaction=True)
    lock._owned_connection.transaction_released = False


def test_double_check_write_reentrance(lock_file: str, mocker: MockerFixture) -> None:
    lock = ReadWriteLock(lock_file, is_singleton=False)
    real_lock = lock._transaction_lock
    mock_lock = mocker.create_autospec(real_lock)

    def fake_acquire(blocking: bool = True, timeout: float = -1) -> bool:
        lock._hold.level = 1
        lock._hold.mode = "write"
        lock._hold.write_thread_id = threading.get_ident()
        _fake_open_transaction(lock, mocker)
        return real_lock.acquire(blocking, timeout)

    mock_lock.acquire = fake_acquire
    mock_lock.release = real_lock.release
    lock._transaction_lock = mock_lock
    proxy = lock.acquire_write()
    assert lock._hold.level == 2
    assert proxy is not None
    lock._hold.level = 0
    lock._hold.mode = None
    lock._owned_connection.connection, lock._owned_connection.transaction_released = None, True
    lock._hold.write_thread_id = None
    lock.release(force=True)


@pytest.mark.parametrize(
    ("acquire_mode", "conflicting_mode", "match"),
    [
        pytest.param("read", "write", "another thread holds a write lock", id="read-blocked-by-write"),
        pytest.param("write", "read", "another thread holds a read lock", id="write-blocked-by-read"),
    ],
)
def test_double_check_wrong_mode_raises(
    lock_file: str,
    mocker: MockerFixture,
    acquire_mode: Literal["read", "write"],
    conflicting_mode: Literal["read", "write"],
    match: str,
) -> None:
    lock = ReadWriteLock(lock_file, is_singleton=False)
    real_lock = lock._transaction_lock
    mock_lock = mocker.create_autospec(real_lock)

    def fake_acquire(blocking: bool = True, timeout: float = -1) -> bool:
        lock._hold.level = 1
        lock._hold.mode = conflicting_mode
        _fake_open_transaction(lock, mocker)
        return real_lock.acquire(blocking, timeout)

    mock_lock.acquire = fake_acquire
    mock_lock.release = real_lock.release
    lock._transaction_lock = mock_lock
    with pytest.raises(RuntimeError, match=match):
        getattr(lock, f"acquire_{acquire_mode}")()
    lock._hold.level = 0
    lock._hold.mode = None
    lock._owned_connection.connection, lock._owned_connection.transaction_released = None, True
