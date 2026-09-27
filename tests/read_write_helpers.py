from __future__ import annotations

import multiprocessing
from typing import TYPE_CHECKING, Final, Literal

import pytest

from filelock import ReadWriteLock, Timeout

if TYPE_CHECKING:
    from multiprocessing.sharedctypes import Synchronized


ACQUIRE_SETTINGS: Final[pytest.MarkDecorator] = pytest.mark.parametrize(
    ("instance_timeout", "instance_blocking", "call_timeout", "call_blocking", "minimum", "omitted"),
    [
        pytest.param(0.2, True, None, None, 0.1, True, id="instance-timeout"),
        pytest.param(30, False, None, None, 0, True, id="instance-nonblocking"),
        pytest.param(0.2, True, None, None, 0.1, False, id="none-timeout"),
        pytest.param(30, False, None, None, 0, False, id="none-nonblocking"),
        pytest.param(30, True, 0.2, None, 0.1, False, id="call-timeout"),
        pytest.param(0.2, False, None, True, 0.1, False, id="call-blocking"),
        pytest.param(30, True, None, False, 0, False, id="call-nonblocking"),
        pytest.param(30, True, 0, None, 0, False, id="zero-timeout"),
    ],
)


def assert_read_write_lock_state(lock_file: str, mode: Literal["read", "write"], *, available: bool) -> None:
    context = multiprocessing.get_context("spawn")
    acquired = context.Value("b", False)
    probe = context.Process(target=_probe_read_write_lock, args=(lock_file, mode, acquired))
    probe.start()
    try:
        probe.join(timeout=5)
        assert not probe.is_alive(), "read-write lock probe did not exit"
        assert (probe.exitcode, acquired.value) == (0, available)
    finally:
        if probe.is_alive():  # pragma: no cover - cleanup for a hung child after the assertion fails
            probe.terminate()
            probe.join(timeout=5)
        probe.close()


def _probe_read_write_lock(lock_file: str, mode: Literal["read", "write"], acquired: Synchronized[bool]) -> None:
    lock = ReadWriteLock(lock_file, is_singleton=False)
    try:
        (lock.acquire_read if mode == "read" else lock.acquire_write)(blocking=False)
    except Timeout:
        return
    acquired.value = True
    lock.release()
    lock.close()


__all__ = ["ACQUIRE_SETTINGS", "assert_read_write_lock_state"]
