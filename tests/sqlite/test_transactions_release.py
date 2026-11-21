from __future__ import annotations

import time
from multiprocessing import Process
from typing import TYPE_CHECKING, Final, Literal

import pytest

pytest.importorskip("sqlite3")
import sqlite3

from filelock import ReadWriteLock, Timeout
from filelock._sqlite._connection import ForkSafeConnection
from tests.capability_marks import SKIP_ON_UNRELIABLE_PROCESS_SYNC
from tests.process_helpers import cleanup_processes
from tests.read_write_helpers import Interrupt, assert_read_write_lock_state, interrupt_once

if TYPE_CHECKING:
    from collections.abc import Callable

    from pytest_mock import MockerFixture


pytestmark: Final[pytest.MarkDecorator] = SKIP_ON_UNRELIABLE_PROCESS_SYNC


@pytest.mark.parametrize(
    ("held_mode", "probe_mode"),
    [
        pytest.param("read", "write", id="read"),
        pytest.param("write", "read", id="write"),
    ],
)
def test_release_rollback_failure_with_open_transaction_keeps_the_lock(
    lock_file: str,
    mocker: MockerFixture,
    held_mode: Literal["read", "write"],
    probe_mode: Literal["read", "write"],
) -> None:
    lock = ReadWriteLock(lock_file, is_singleton=False)
    getattr(lock, f"acquire_{held_mode}")()
    rollback_error = _patch_rollback_failure(mocker, after_rollback=False)

    with pytest.raises(sqlite3.OperationalError, match="rollback failed") as info:
        lock.release()
    assert info.value is rollback_error
    assert_read_write_lock_state(lock_file, probe_mode, available=False)

    lock.release()
    assert_read_write_lock_state(lock_file, probe_mode, available=True)
    lock.close()


def _patch_rollback_failure(mocker: MockerFixture, *, after_rollback: bool) -> sqlite3.OperationalError:
    error = sqlite3.OperationalError("rollback failed")
    failed = False

    def rollback(connection: ForkSafeConnection) -> None:
        nonlocal failed
        if failed:
            sqlite3.Connection.rollback(connection)
            return
        failed = True
        if after_rollback:
            sqlite3.Connection.rollback(connection)
        raise error

    mocker.patch.object(ForkSafeConnection, "rollback", new=rollback)
    return error


@pytest.mark.parametrize(
    ("held_mode", "probe_mode"),
    [
        pytest.param("read", "write", id="read"),
        pytest.param("write", "read", id="write"),
    ],
)
def test_release_rollback_failure_after_transaction_end_reports_not_held(
    lock_file: str,
    mocker: MockerFixture,
    held_mode: Literal["read", "write"],
    probe_mode: Literal["read", "write"],
) -> None:
    lock = ReadWriteLock(lock_file, is_singleton=False)
    getattr(lock, f"acquire_{held_mode}")()
    rollback_error = _patch_rollback_failure(mocker, after_rollback=True)

    with pytest.raises(sqlite3.OperationalError, match="rollback failed") as info:
        lock.release()
    assert info.value is rollback_error
    assert_read_write_lock_state(lock_file, probe_mode, available=True)

    with pytest.raises(RuntimeError, match="not held"):
        lock.release()
    lock.close()


@pytest.mark.parametrize("mode", [pytest.param("read", id="read"), pytest.param("write", id="write")])
def test_acquire_proxy_releases_the_read_write_lock_on_exit(lock_file: str, mode: Literal["read", "write"]) -> None:
    # A read/write lock owns its release policy even when a shared proxy wraps it.

    with getattr(ReadWriteLock(lock_file, is_singleton=False), f"acquire_{mode}")():
        assert_read_write_lock_state(lock_file, "write", available=False)

    assert_read_write_lock_state(lock_file, "write", available=True)


def test_cleanup_reports_the_failure_that_escaped_before_a_start() -> None:
    unstarted = Process(target=time.sleep, args=(0,))

    def fail_before_start() -> None:
        with cleanup_processes([unstarted]):
            msg = "the real failure"
            raise AssertionError(msg)

    with pytest.raises(AssertionError, match="the real failure"):
        fail_before_start()


@pytest.mark.parametrize("mode", [pytest.param("read", id="read"), pytest.param("write", id="write")])
@pytest.mark.parametrize(
    ("owner", "name", "real", "before"),
    [
        pytest.param(ForkSafeConnection, "rollback", sqlite3.Connection.rollback, False, id="after-rollback"),
        pytest.param(ForkSafeConnection, "close", ForkSafeConnection.close, False, id="after-close"),
        pytest.param(ReadWriteLock, "_clear_lock_state", ReadWriteLock._clear_lock_state, True, id="at-level-reset"),
    ],
)
def test_read_write_lock_after_an_interrupted_release_locks_again(
    lock_file: str,
    mocker: MockerFixture,
    mode: Literal["read", "write"],
    owner: type,
    name: str,
    real: Callable[[sqlite3.Connection | ReadWriteLock], None],
    before: bool,
) -> None:
    lock: Final[ReadWriteLock] = ReadWriteLock(lock_file, is_singleton=False)
    acquire = getattr(lock, f"acquire_{mode}")
    acquire()
    mocker.patch.object(owner, name, new=interrupt_once(real, before=before))
    with pytest.raises(Interrupt):
        lock.release()

    acquire()

    contender: Final[ReadWriteLock] = ReadWriteLock(lock_file, is_singleton=False)
    with pytest.raises(Timeout):
        contender.acquire_write(blocking=False)
    lock.release()
    contender.close()
    lock.close()
