from __future__ import annotations

import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from errno import EIO
from types import SimpleNamespace
from typing import TYPE_CHECKING, Final

import pytest

from filelock import (
    FileLock,
    Timeout,
    lock_descriptor,
    unlock_descriptor,
)
from tests.capability_marks import (
    NEEDS_FCNTL,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from pytest_mock import MockerFixture

_INVALID_DESCRIPTOR_POLL_INTERVALS: Final[tuple[pytest.ParameterSet, ...]] = (
    pytest.param(0.0, id="zero"),
    pytest.param(-0.01, id="negative"),
    pytest.param(float("nan"), id="nan"),
    pytest.param(float("inf"), id="positive-infinity"),
    pytest.param(float("-inf"), id="negative-infinity"),
    pytest.param(threading.TIMEOUT_MAX * 2, id="above-timeout-max"),
)


def test_lock_descriptor_roundtrip(tmp_path: Path) -> None:
    fd = os.open(str(tmp_path / "a"), os.O_RDWR | os.O_CREAT)
    try:
        assert lock_descriptor(fd, blocking=False) is True
        os.write(fd, b"held")
        unlock_descriptor(fd)
        os.lseek(fd, 0, os.SEEK_SET)
        assert os.read(fd, 4) == b"held"
    finally:
        os.close(fd)


def test_lock_descriptor_nonblocking_contention(tmp_path: Path) -> None:
    path = str(tmp_path / "a")
    holder = os.open(path, os.O_RDWR | os.O_CREAT)
    contender = os.open(path, os.O_RDWR | os.O_CREAT)
    try:
        assert lock_descriptor(holder, blocking=False) is True
        assert lock_descriptor(contender, blocking=False) is False
        unlock_descriptor(holder)
        assert lock_descriptor(contender, blocking=False) is True
        unlock_descriptor(contender)
    finally:
        os.close(holder)
        os.close(contender)


def test_lock_descriptor_invalid_fd_raises(tmp_path: Path) -> None:
    fd = os.open(str(tmp_path / "a"), os.O_RDWR | os.O_CREAT)
    os.close(fd)
    with pytest.raises(OSError, match=r"Bad file descriptor|not open|invalid"):
        lock_descriptor(fd, blocking=False)


@pytest.mark.parametrize(
    "blocking", [pytest.param("false", id="str"), pytest.param(0, id="int"), pytest.param(1, id="int-one")]
)
def test_lock_descriptor_rejects_non_bool_blocking(blocking: str | int) -> None:
    with pytest.raises(TypeError, match="blocking must be a bool"):
        lock_descriptor(-1, blocking=blocking)  # ty: ignore[invalid-argument-type]  # the rejected type is the contract


@pytest.mark.parametrize("poll_interval", [pytest.param(True, id="bool"), pytest.param("0.05", id="str")])
def test_lock_descriptor_rejects_non_number_blocking_poll_interval(poll_interval: bool | str) -> None:
    with pytest.raises(TypeError, match="poll_interval must be a number of seconds"):
        lock_descriptor(-1, poll_interval=poll_interval)  # ty: ignore[invalid-argument-type]  # the rejected type is the contract


@pytest.mark.parametrize(
    "poll_interval",
    _INVALID_DESCRIPTOR_POLL_INTERVALS,
)
def test_lock_descriptor_rejects_invalid_blocking_poll_interval(poll_interval: float) -> None:
    with pytest.raises(ValueError, match="poll_interval must be greater than 0 and at most"):
        lock_descriptor(-1, poll_interval=poll_interval)


@pytest.mark.parametrize(
    "poll_interval",
    _INVALID_DESCRIPTOR_POLL_INTERVALS,
)
def test_lock_descriptor_nonblocking_ignores_poll_interval(tmp_path: Path, poll_interval: float) -> None:
    fd = os.open(str(tmp_path / "a"), os.O_RDWR | os.O_CREAT)
    try:
        assert lock_descriptor(fd, blocking=False, poll_interval=poll_interval) is True
        unlock_descriptor(fd)
    finally:
        os.close(fd)


def test_filelock_first_blocks_descriptor_until_released(tmp_path: Path) -> None:
    path = str(tmp_path / "a")
    lock = FileLock(path)
    fd = os.open(path, os.O_RDWR | os.O_CREAT)
    try:
        lock.acquire()
        assert lock_descriptor(fd, blocking=False) is False
        lock.release()
        assert lock_descriptor(fd, blocking=False) is True
        unlock_descriptor(fd)
    finally:
        os.close(fd)


def test_descriptor_first_blocks_filelock_until_released(tmp_path: Path) -> None:
    path = str(tmp_path / "a")
    lock = FileLock(path)
    fd = os.open(path, os.O_RDWR | os.O_CREAT)
    try:
        assert lock_descriptor(fd, blocking=False) is True
        with pytest.raises(Timeout):
            lock.acquire(timeout=0.2)
        unlock_descriptor(fd)
        lock.acquire()
        lock.release()
    finally:
        os.close(fd)


@NEEDS_FCNTL  # pragma: needs fcntl
def test_lock_descriptor_touches_no_paths(tmp_path: Path) -> None:
    path = tmp_path / "a"
    fd = os.open(str(path), os.O_RDWR | os.O_CREAT)
    try:
        os.write(fd, b"payload")
        before = os.fstat(fd)
        assert lock_descriptor(fd, blocking=False) is True
        unlock_descriptor(fd)
        after = os.fstat(fd)
        # Checking the inode and contents avoids patching global os calls during unrelated cleanup.
        assert (after.st_ino, after.st_dev, after.st_size, after.st_mode) == (
            before.st_ino,
            before.st_dev,
            before.st_size,
            before.st_mode,
        )
        assert path.read_bytes() == b"payload"
    finally:
        os.close(fd)


@NEEDS_FCNTL  # pragma: needs fcntl
def test_unlock_descriptor_failure_allows_retry(tmp_path: Path, mocker: MockerFixture) -> None:
    fd = os.open(str(tmp_path / "a"), os.O_RDWR | os.O_CREAT)
    try:
        assert lock_descriptor(fd, blocking=False) is True
        mocker.patch(
            "filelock.internal.native._unix.fcntl.flock",
            side_effect=[OSError(EIO, "unlock failed"), None],
            autospec=True,
        )
        with pytest.raises(OSError, match="unlock failed"):
            unlock_descriptor(fd)
        unlock_descriptor(fd)
    finally:
        os.close(fd)


@pytest.mark.parametrize(
    "poll_interval", [pytest.param(0.01, id="short"), pytest.param(threading.TIMEOUT_MAX, id="timeout-max")]
)
def test_lock_descriptor_blocking_retries_until_free(
    tmp_path: Path, mocker: MockerFixture, poll_interval: float
) -> None:
    path = str(tmp_path / "a")
    holder = os.open(path, os.O_RDWR | os.O_CREAT)
    fd = os.open(path, os.O_RDWR | os.O_CREAT)
    assert lock_descriptor(holder, blocking=False) is True
    # Releasing on the first sleep makes the retry deterministic.
    sleep = mocker.create_autospec(time.sleep, side_effect=lambda _: unlock_descriptor(holder))
    mocker.patch("filelock.internal.native._descriptor.time", new=SimpleNamespace(sleep=sleep))
    try:
        assert lock_descriptor(fd, blocking=True, poll_interval=poll_interval) is True
        sleep.assert_called_once_with(poll_interval)
        unlock_descriptor(fd)
    finally:
        os.close(holder)
        os.close(fd)


# NetBSD lacks thread_time. Process CPU time also detects a busy loop.
_CPU_TIME: Final[Callable[[], float]] = getattr(time, "thread_time", time.process_time)


def test_lock_descriptor_blocking_wait_does_not_spin(tmp_path: Path) -> None:
    path = str(tmp_path / "a")
    holder = os.open(path, os.O_RDWR | os.O_CREAT)
    contender = os.open(path, os.O_RDWR | os.O_CREAT)
    assert lock_descriptor(holder, blocking=False) is True
    started = threading.Event()

    def acquire() -> float:
        started.set()
        before = _CPU_TIME()
        lock_descriptor(contender, poll_interval=0.01)
        return _CPU_TIME() - before

    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(acquire)
            try:
                assert started.wait(timeout=1)
                time.sleep(0.2)  # simplint: ignore[sleep-in-test]  # needs a poll-entered signal
            finally:
                unlock_descriptor(holder)
            assert future.result(timeout=2) < 0.05
            unlock_descriptor(contender)
    finally:
        os.close(holder)
        os.close(contender)
