from __future__ import annotations

import multiprocessing
from typing import TYPE_CHECKING, Final, Literal

import pytest

from filelock import ReadWriteLock, Timeout

if TYPE_CHECKING:
    import sys

    if sys.platform == "win32":
        from multiprocessing.connection import PipeConnection as Connection
    else:
        from multiprocessing.connection import Connection


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
    context: Final = multiprocessing.get_context("spawn")
    # Pipes avoid GraalPy's multiprocessing semaphore failures.
    receiving: Final[Connection]
    sending: Final[Connection]
    receiving, sending = context.Pipe(duplex=False)
    probe: Final = context.Process(target=_probe_read_write_lock, args=(lock_file, mode, sending))
    probe.start()
    sending.close()
    try:
        # Spawn imports and coverage shutdown need separate budgets from the lock operation.
        assert receiving.poll(timeout=10), "read-write lock probe did not start"
        receiving.recv_bytes()
        assert receiving.poll(timeout=5), "read-write lock probe did not finish"
        acquired: Final = receiving.recv_bytes()
        probe.join(timeout=10)
        assert not probe.is_alive(), "read-write lock probe did not exit"
        assert (probe.exitcode, acquired) == (0, bytes([available]))
    finally:
        if probe.is_alive():  # pragma: no cover - cleanup for a hung child after the assertion fails
            probe.terminate()
            probe.join(timeout=5)
        probe.close()
        receiving.close()


def _probe_read_write_lock(lock_file: str, mode: Literal["read", "write"], sending: Connection) -> None:
    with sending:
        lock: Final = ReadWriteLock(lock_file, is_singleton=False)
        sending.send_bytes(b"")
        acquired = False
        try:
            try:
                (lock.acquire_read if mode == "read" else lock.acquire_write)(blocking=False)
            except Timeout:
                return
            acquired = True
            lock.release()
        finally:
            lock.close()
            sending.send_bytes(bytes([acquired]))


__all__ = ["ACQUIRE_SETTINGS", "assert_read_write_lock_state"]
