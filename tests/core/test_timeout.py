from __future__ import annotations

import logging
import threading
import time
from inspect import getframeinfo, stack
from itertools import count
from types import SimpleNamespace
from typing import TYPE_CHECKING, Literal, cast

import pytest

from filelock import (
    BaseFileLock,
    FileLock,
    SoftFileLock,
    Timeout,
)
from filelock._core import acquire_thread_lock

if TYPE_CHECKING:
    from pathlib import Path

    from pytest_mock import MockerFixture, MockType


@pytest.mark.parametrize("lock_type", [FileLock, SoftFileLock])
def test_timeout(lock_type: type[BaseFileLock], tmp_path: Path) -> None:
    lock_path = tmp_path / "a"
    lock_1, lock_2 = lock_type(str(lock_path)), lock_type(str(lock_path))

    lock_1.acquire()
    assert lock_1.is_locked
    assert not lock_2.is_locked

    with pytest.raises(Timeout, match=r"The file lock '.*' could not be acquired."):
        lock_2.acquire(timeout=0.1)
    assert not lock_2.is_locked
    assert lock_1.is_locked

    lock_1.release()
    assert not lock_1.is_locked
    assert not lock_2.is_locked


@pytest.mark.parametrize("lock_type", [FileLock, SoftFileLock])
def test_non_blocking(lock_type: type[BaseFileLock], tmp_path: Path) -> None:
    path = tmp_path / "a"
    holder = lock_type(path)
    contender = lock_type(path)
    peers = (lock_type(path, blocking=False), lock_type(path, timeout=0), lock_type(path, blocking=False, timeout=-1))
    message = r"The file lock '.*' could not be acquired."

    with holder:
        assert holder.is_locked
        assert not contender.is_locked
        assert not any(peer.is_locked for peer in peers)
        with pytest.raises(Timeout, match=message):
            contender.acquire(blocking=False)
        assert (contender.is_locked, holder.is_locked) == (False, True)

        for peer in peers:
            with pytest.raises(Timeout, match=message):
                peer.acquire()
            assert (peer.is_locked, holder.is_locked) == (False, True)
            with pytest.raises(Timeout, match=message), peer:
                pass  # pragma: no cover  # entering the context raises Timeout
            assert (peer.is_locked, holder.is_locked) == (False, True)

    assert not any(lock.is_locked for lock in (holder, contender, *peers))


@pytest.mark.parametrize("lock_type", [FileLock, SoftFileLock])
def test_default_timeout(lock_type: type[BaseFileLock], tmp_path: Path) -> None:
    lock_path = tmp_path / "a"
    lock_1, lock_2 = lock_type(str(lock_path)), lock_type(str(lock_path), timeout=0.1)
    assert lock_2.timeout == pytest.approx(0.1)

    lock_1.acquire()
    assert lock_1.is_locked
    assert not lock_2.is_locked

    with pytest.raises(Timeout, match=r"The file lock '.*' could not be acquired."):
        lock_2.acquire()
    assert not lock_2.is_locked
    assert lock_1.is_locked

    lock_2.timeout = 0
    assert lock_2.timeout == 0

    with pytest.raises(Timeout, match=r"The file lock '.*' could not be acquired."):
        lock_2.acquire()
    assert not lock_2.is_locked
    assert lock_1.is_locked

    lock_1.release()
    assert not lock_1.is_locked
    assert not lock_2.is_locked


@pytest.mark.parametrize("lock_type", [FileLock, SoftFileLock])
def test_poll_intervall_deprecated(lock_type: type[BaseFileLock], tmp_path: Path) -> None:
    lock = lock_type(str(tmp_path / "a"))

    with pytest.deprecated_call(match="use poll_interval instead of poll_intervall") as checker:
        lock.acquire(poll_intervall=0.05)
        frame_info = getframeinfo(stack()[0][0])  # lineno here is one past the acquire() call above
        matching = [w for w in checker if w.filename == frame_info.filename and w.lineno + 1 == frame_info.lineno]
        assert matching, "No warnings of stacklevel=2 matching."
    # Deferred finalization can send this release to a later test's log.
    lock.release()


@pytest.mark.parametrize("lock_type", [FileLock, SoftFileLock])
def test_default_poll_interval(lock_type: type[BaseFileLock], tmp_path: Path) -> None:
    lock_path = tmp_path / "a"
    lock = lock_type(str(lock_path))
    assert lock.poll_interval == pytest.approx(0.05)

    lock_2 = lock_type(str(lock_path), poll_interval=0.1)
    assert lock_2.poll_interval == pytest.approx(0.1)

    lock_2.poll_interval = 0.2
    assert lock_2.poll_interval == pytest.approx(0.2)

    lock_3 = lock_type(str(lock_path), poll_interval=0)
    assert lock_3.poll_interval == 0
    lock_3.poll_interval = 0.0
    assert lock_3.poll_interval == pytest.approx(0.0)


@pytest.fixture
def steady_clock(mocker: MockerFixture) -> MockType:
    # Advancing 10 ms per read keeps the first poll inside the timeout even on a loaded runner.
    sleep_mock = mocker.create_autospec(time.sleep)
    clock = SimpleNamespace(
        perf_counter=mocker.create_autospec(time.perf_counter, side_effect=(reading / 100 for reading in count())),
        sleep=sleep_mock,
    )
    mocker.patch("filelock._core._state.time", new=clock)
    mocker.patch("filelock._core._acquire.time", new=clock)
    return sleep_mock


@pytest.mark.parametrize("lock_type", [FileLock, SoftFileLock])
def test_poll_interval_used_by_context_manager(
    lock_type: type[BaseFileLock], tmp_path: Path, steady_clock: MockType
) -> None:
    lock_path = tmp_path / "a"
    lock_1 = lock_type(str(lock_path))
    lock_2 = lock_type(str(lock_path), timeout=0.2, poll_interval=0.05)

    lock_1.acquire()
    with pytest.raises(Timeout):
        lock_2.acquire()
    steady_clock.assert_any_call(0.05)

    steady_clock.reset_mock()
    lock_2.poll_interval = 0.1
    with pytest.raises(Timeout):
        lock_2.acquire()
    steady_clock.assert_any_call(0.1)
    lock_1.release()


@pytest.mark.parametrize("lock_type", [FileLock, SoftFileLock])
def test_poll_interval_acquire_override(lock_type: type[BaseFileLock], tmp_path: Path, steady_clock: MockType) -> None:
    lock_path = tmp_path / "a"
    lock_1 = lock_type(str(lock_path))
    lock_2 = lock_type(str(lock_path), timeout=0.2, poll_interval=0.05)

    lock_1.acquire()
    with pytest.raises(Timeout):
        lock_2.acquire(poll_interval=0.15)
    steady_clock.assert_any_call(0.15)
    lock_1.release()


@pytest.mark.parametrize("lock_type", [FileLock, SoftFileLock])
@pytest.mark.parametrize("entry_point", ["constructor", "setter", "acquire"])
@pytest.mark.parametrize(
    ("bad_value", "error_type", "message"),
    [
        pytest.param(-1, ValueError, "finite and non-negative", id="negative-int"),
        pytest.param(-0.5, ValueError, "finite and non-negative", id="negative-float"),
        pytest.param(float("nan"), ValueError, "finite and non-negative", id="nan"),
        pytest.param(float("inf"), ValueError, "finite and non-negative", id="positive-infinity"),
        pytest.param(float("-inf"), ValueError, "finite and non-negative", id="negative-infinity"),
        pytest.param(threading.TIMEOUT_MAX * 2, ValueError, "TIMEOUT_MAX", id="above-timeout-max"),
        pytest.param(True, TypeError, "poll_interval must be", id="true"),
        pytest.param(False, TypeError, "poll_interval must be", id="false"),
        pytest.param("5", TypeError, "poll_interval must be", id="string"),
    ],
)
def test_poll_interval_rejects_invalid_value(
    lock_type: type[BaseFileLock],
    entry_point: Literal["constructor", "setter", "acquire"],
    bad_value: float | str,
    error_type: type[ValueError | TypeError],
    message: str,
    tmp_path: Path,
) -> None:
    lock = lock_type(str(tmp_path / "a"))
    with pytest.raises(error_type, match=message):
        _set_poll_interval(lock_type, entry_point, lock, bad_value)


def _set_poll_interval(
    lock_type: type[BaseFileLock],
    entry_point: Literal["constructor", "setter", "acquire"],
    lock: BaseFileLock,
    value: float | str,
) -> None:
    value = cast("float", value)
    if entry_point == "constructor":
        lock_type(lock.lock_file, poll_interval=value)
    elif entry_point == "setter":
        lock.poll_interval = value
    else:
        lock.acquire(poll_interval=value, timeout=0)


def test_thread_lock_checks_cancellation_after_repolling() -> None:
    gate = threading.Lock()
    polls = 0

    def cancel_after_two_polls() -> bool:
        nonlocal polls
        polls += 1
        return polls == 2

    with gate:
        assert not acquire_thread_lock(
            gate, blocking=True, timeout=-1, poll_interval=0, cancel_check=cancel_after_two_polls
        )
    assert polls == 2


@pytest.mark.parametrize("lock_type", [FileLock, SoftFileLock])
def test_cancel_check_triggers(lock_type: type[BaseFileLock], tmp_path: Path) -> None:
    lock_path = tmp_path / "a"
    lock_1 = lock_type(str(lock_path))
    lock_2 = lock_type(str(lock_path))

    lock_1.acquire()

    with pytest.raises(Timeout, match=r"The file lock '.*' could not be acquired."):
        lock_2.acquire(timeout=1, cancel_check=lambda: True)
    assert not lock_2.is_locked
    lock_1.release()


@pytest.mark.parametrize("lock_type", [FileLock, SoftFileLock])
def test_cancel_check_after_n_polls(lock_type: type[BaseFileLock], tmp_path: Path, mocker: MockerFixture) -> None:
    lock_path = tmp_path / "a"
    lock_1 = lock_type(str(lock_path))
    lock_2 = lock_type(str(lock_path))

    lock_1.acquire()

    call_count = 0

    def cancel_after_two() -> bool:
        nonlocal call_count
        call_count += 1
        return call_count >= 2

    mocker.patch(
        "filelock._core._acquire.time",
        new=SimpleNamespace(perf_counter=time.perf_counter, sleep=mocker.create_autospec(time.sleep)),
    )
    with pytest.raises(Timeout):
        lock_2.acquire(timeout=10, cancel_check=cancel_after_two)
    assert call_count == 2
    assert not lock_2.is_locked
    lock_1.release()


@pytest.mark.parametrize("lock_type", [FileLock, SoftFileLock])
def test_cancel_check_not_called_when_lock_available(lock_type: type[BaseFileLock], tmp_path: Path) -> None:
    lock = lock_type(str(tmp_path / "a"))

    called = False

    def should_not_be_called() -> bool:  # pragma: no cover  # a free lock never consults cancel_check
        nonlocal called
        called = True
        return True

    lock.acquire(cancel_check=should_not_be_called)
    assert lock.is_locked
    assert not called
    lock.release()


@pytest.mark.parametrize("lock_type", [FileLock, SoftFileLock])
def test_cancel_check_false_allows_acquisition(lock_type: type[BaseFileLock], tmp_path: Path) -> None:
    lock = lock_type(str(tmp_path / "a"))

    lock.acquire(cancel_check=lambda: False)
    assert lock.is_locked
    lock.release()


@pytest.mark.parametrize("lock_type", [FileLock, SoftFileLock])
def test_cancel_check_log_message(
    lock_type: type[BaseFileLock], tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    lock_path = tmp_path / "a"
    lock_1 = lock_type(str(lock_path))
    lock_2 = lock_type(str(lock_path))

    lock_1.acquire()
    with pytest.raises(Timeout):
        lock_2.acquire(timeout=1, cancel_check=lambda: True)
    assert any("Cancellation requested" in msg for msg in caplog.messages)
    lock_1.release()
