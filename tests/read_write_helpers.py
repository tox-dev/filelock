from __future__ import annotations

import multiprocessing
from typing import TYPE_CHECKING, Literal

from filelock import ReadWriteLock, Timeout

if TYPE_CHECKING:
    from multiprocessing.sharedctypes import Synchronized


def assert_read_write_lock_state(lock_file: str, mode: Literal["read", "write"], *, available: bool) -> None:
    context = multiprocessing.get_context("spawn")
    acquired = context.Value("b", False)
    probe = context.Process(target=_probe_read_write_lock, args=(lock_file, mode, acquired))
    probe.start()
    try:
        probe.join(timeout=5)
        assert not probe.is_alive(), "read-write lock probe did not exit"
    finally:
        if probe.is_alive():  # pragma: no cover - cleanup for a hung child after the assertion fails
            probe.terminate()
            probe.join(timeout=5)
    assert (probe.exitcode, acquired.value) == (0, available)


def _probe_read_write_lock(lock_file: str, mode: Literal["read", "write"], acquired: Synchronized[bool]) -> None:
    lock = ReadWriteLock(lock_file, is_singleton=False)
    try:
        (lock.acquire_read if mode == "read" else lock.acquire_write)(blocking=False)
    except Timeout:
        return
    acquired.value = True
    lock.release()
    lock.close()


__all__ = ["assert_read_write_lock_state"]
