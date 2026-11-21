from __future__ import annotations

import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from typing import TYPE_CHECKING, Final, Literal

import pytest

from tests.read_write_helpers import ACQUIRE_SETTINGS, INVALID_TIMEOUTS

pytest.importorskip("sqlite3")

from filelock import ReadWriteLock, SoftReadWriteLock, Timeout

if TYPE_CHECKING:
    from collections.abc import Generator
    from pathlib import Path


pytestmark: Final[pytest.MarkDecorator] = pytest.mark.usefixtures("clear_sqlite_singletons")


@pytest.mark.parametrize("mode", [pytest.param("read", id="read"), pytest.param("write", id="write")])
@ACQUIRE_SETTINGS
def test_acquire_respects_settings(
    sync_locks: tuple[ReadWriteLock, ReadWriteLock],
    mode: Literal["read", "write"],
    instance_timeout: float,
    call_timeout: float | None,
    minimum: float,
    *,
    instance_blocking: bool,
    call_blocking: bool | None,
) -> None:
    contender: Final[ReadWriteLock] = sync_locks[1]
    contender.timeout = instance_timeout
    contender.blocking = instance_blocking
    acquire = getattr(contender, f"acquire_{mode}")
    with sync_locks[0].write_lock():
        started: Final[float] = time.perf_counter()
        with pytest.raises(Timeout):
            acquire(timeout=call_timeout, blocking=call_blocking)
        assert minimum <= time.perf_counter() - started < 5


@pytest.mark.parametrize("mode", [pytest.param("read", id="read"), pytest.param("write", id="write")])
@pytest.mark.parametrize(
    ("instance_timeout", "instance_blocking", "minimum"),
    [
        pytest.param(0.2, True, 0.1, id="instance-timeout"),
        pytest.param(30, False, 0, id="instance-nonblocking"),
    ],
)
def test_acquire_without_arguments_uses_instance_settings(
    sync_locks: tuple[ReadWriteLock, ReadWriteLock],
    mode: Literal["read", "write"],
    instance_timeout: float,
    instance_blocking: bool,
    minimum: float,
) -> None:
    contender: Final[ReadWriteLock] = sync_locks[1]
    contender.timeout = instance_timeout
    contender.blocking = instance_blocking
    acquire = getattr(contender, f"acquire_{mode}")
    with sync_locks[0].write_lock():
        started: Final[float] = time.perf_counter()
        with pytest.raises(Timeout):
            acquire()
        assert minimum <= time.perf_counter() - started < 5


def test_contended_acquire_beside_another_thread_times_out_on_time(
    sync_locks: tuple[ReadWriteLock, ReadWriteLock],
) -> None:
    holder, contender = sync_locks
    done: Final[threading.Event] = threading.Event()
    # With another thread alive the wait runs in short attempts, which together still honor the timeout.
    other: Final[threading.Thread] = threading.Thread(target=done.wait)
    other.start()
    try:
        started = time.perf_counter()
        with holder.write_lock(), pytest.raises(Timeout):
            contender.acquire_write(timeout=0.2)
        # SQLite counts its busy timeout in whole milliseconds, so the last attempt can end a little early.
        assert 0.15 <= time.perf_counter() - started < 5
    finally:
        done.set()
        other.join()


def test_unlimited_acquire_beside_another_thread_waits_for_the_holder(
    sync_locks: tuple[ReadWriteLock, ReadWriteLock],
) -> None:
    holder, contender = sync_locks
    holder.acquire_write()
    with ThreadPoolExecutor(max_workers=1) as executor:
        acquired = executor.submit(contender.acquire_read, -1)
        with pytest.raises(TimeoutError):
            acquired.result(timeout=0.2)
        holder.release()
        acquired.result(timeout=5)
        executor.submit(contender.release).result(timeout=5)


@pytest.mark.parametrize("mode", [pytest.param("read", id="read"), pytest.param("write", id="write")])
def test_acquire_default_excludes_contender(
    sync_locks: tuple[ReadWriteLock, ReadWriteLock], mode: Literal["read", "write"]
) -> None:
    holder: Final[ReadWriteLock] = sync_locks[0]
    contender: Final[ReadWriteLock] = sync_locks[1]
    with getattr(holder, f"acquire_{mode}")(), pytest.raises(Timeout):
        contender.acquire_write(blocking=False)
    with contender.acquire_write(), pytest.raises(Timeout):
        holder.acquire_write(blocking=False)


@pytest.mark.parametrize("mode", [pytest.param("read", id="read"), pytest.param("write", id="write")])
@pytest.mark.parametrize("blocking", [pytest.param("false", id="str"), pytest.param(0, id="int")])
def test_acquire_rejects_a_blocking_that_is_not_a_bool(
    timeout_locks: tuple[ReadWriteLock | SoftReadWriteLock, ReadWriteLock | SoftReadWriteLock],
    mode: Literal["read", "write"],
    blocking: str | int,
) -> None:
    # Contended, so a 0 read as blocking would wait out the contender's timeout instead of failing fast.
    holder, contender = timeout_locks
    acquire = getattr(contender, f"acquire_{mode}")
    with holder.acquire_write(blocking=False):
        started: Final[float] = time.monotonic()
        with pytest.raises(TypeError, match="blocking must be a bool"):
            acquire(blocking=blocking)
        assert time.monotonic() - started < 5
    with contender.acquire_write(blocking=False):
        pass


@pytest.mark.parametrize("mode", [pytest.param("read", id="read"), pytest.param("write", id="write")])
@INVALID_TIMEOUTS
def test_acquire_rejects_invalid_timeout(
    timeout_locks: tuple[ReadWriteLock | SoftReadWriteLock, ReadWriteLock | SoftReadWriteLock],
    mode: Literal["read", "write"],
    timeout: float,
    message: str,
) -> None:
    holder: Final[ReadWriteLock | SoftReadWriteLock] = timeout_locks[0]
    acquire = getattr(holder, f"acquire_{mode}")
    with pytest.raises(ValueError, match=f"^{re.escape(message)}$"):
        acquire(timeout=timeout)
    with timeout_locks[1].acquire_write(blocking=False), pytest.raises(Timeout):
        holder.acquire_write(blocking=False)


@pytest.mark.parametrize("mode", [pytest.param("read", id="read"), pytest.param("write", id="write")])
@INVALID_TIMEOUTS
def test_reentrant_acquire_rejects_invalid_timeout(
    timeout_locks: tuple[ReadWriteLock | SoftReadWriteLock, ReadWriteLock | SoftReadWriteLock],
    mode: Literal["read", "write"],
    timeout: float,
    message: str,
) -> None:
    holder: Final[ReadWriteLock | SoftReadWriteLock] = timeout_locks[0]
    acquire = getattr(holder, f"acquire_{mode}")
    with acquire(), pytest.raises(ValueError, match=f"^{re.escape(message)}$"):
        acquire(timeout=timeout)
    with timeout_locks[1].acquire_write(blocking=False), pytest.raises(Timeout):
        holder.acquire_write(blocking=False)


@pytest.mark.parametrize("mode", [pytest.param("read", id="read"), pytest.param("write", id="write")])
@pytest.mark.parametrize(
    ("timeout", "blocking"),
    [
        pytest.param(-2, False, id="nonblocking-integer"),
        pytest.param(-0.5, False, id="nonblocking-fraction"),
        pytest.param(float("-inf"), False, id="nonblocking-negative-infinite"),
        pytest.param(float("inf"), False, id="nonblocking-infinite"),
        pytest.param(float("nan"), False, id="nonblocking-nan"),
        pytest.param(-1, True, id="unlimited"),
        pytest.param(float("inf"), True, id="infinite"),
        pytest.param(threading.TIMEOUT_MAX * 2, True, id="above-lock-limit"),
        pytest.param(0, True, id="immediate"),
    ],
)
def test_acquire_valid_timeout_preserves_nested_hold(
    timeout_locks: tuple[ReadWriteLock | SoftReadWriteLock, ReadWriteLock | SoftReadWriteLock],
    mode: Literal["read", "write"],
    timeout: float,
    *,
    blocking: bool,
) -> None:
    holder: Final[ReadWriteLock | SoftReadWriteLock] = timeout_locks[0]
    contender: Final[ReadWriteLock | SoftReadWriteLock] = timeout_locks[1]
    acquire = getattr(holder, f"acquire_{mode}")
    with acquire(timeout=timeout, blocking=blocking):
        with acquire(timeout=timeout, blocking=blocking), pytest.raises(Timeout):
            contender.acquire_write(blocking=False)
        with pytest.raises(Timeout):
            contender.acquire_write(blocking=False)
    with contender.acquire_write(blocking=False), pytest.raises(Timeout):
        holder.acquire_write(blocking=False)


@pytest.fixture
def timeout_locks(
    tmp_path: Path, lock_type: type[ReadWriteLock | SoftReadWriteLock]
) -> Generator[tuple[ReadWriteLock | SoftReadWriteLock, ReadWriteLock | SoftReadWriteLock]]:
    with (
        closing(lock_type(tmp_path / "a", is_singleton=False)) as holder,
        closing(lock_type(tmp_path / "a", is_singleton=False)) as contender,
    ):
        yield holder, contender
