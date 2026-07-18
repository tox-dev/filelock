from __future__ import annotations

import importlib
import multiprocessing
import sys
import threading
from typing import TYPE_CHECKING, cast

from filelock import FileLock, Timeout

if TYPE_CHECKING:
    import asyncio
    from multiprocessing.sharedctypes import Synchronized
    from typing import Protocol

    class FcntlModule(Protocol):
        LOCK_UN: int

        @staticmethod
        def flock(fd: int, operation: int) -> None: ...


def assert_cancellation_message(error: asyncio.CancelledError, message: str) -> None:
    # Task.cancel() did not propagate its message to code awaiting the task until Python 3.11.
    assert error.args == ((message,) if sys.version_info >= (3, 11) else ())


def get_fcntl() -> FcntlModule:  # pragma: win32 no cover
    return cast("FcntlModule", importlib.import_module("fcntl"))


def assert_file_lock_state(lock_file: str, *, available: bool) -> None:
    context = multiprocessing.get_context("spawn")
    acquired = context.Value("b", False)
    probe = context.Process(target=_probe_file_lock, args=(lock_file, acquired))
    probe.start()
    try:
        probe.join(timeout=5)
        assert not probe.is_alive(), "lock probe did not exit"
    finally:
        if probe.is_alive():  # pragma: no cover - cleanup for a hung child after the assertion fails
            probe.terminate()
            probe.join(timeout=5)
    assert (probe.exitcode, acquired.value) == (0, available)


def start_file_lock_holder(lock_file: str) -> tuple[threading.Thread, threading.Event, threading.Event]:
    holder_started = threading.Event()
    finish_holder = threading.Event()
    holder = threading.Thread(target=_hold_file_lock, args=(lock_file, holder_started, finish_holder))
    holder.start()
    return holder, holder_started, finish_holder


def _probe_file_lock(lock_file: str, acquired: Synchronized[bool]) -> None:
    try:
        with FileLock(lock_file, timeout=0):
            acquired.value = True
    except Timeout:  # pragma: win32 no cover
        pass  # pragma: win32 no cover


def _hold_file_lock(lock_file: str, holder_started: threading.Event, finish_holder: threading.Event) -> None:
    with FileLock(lock_file):
        holder_started.set()
        assert finish_holder.wait(timeout=5)


__all__ = ["assert_cancellation_message", "assert_file_lock_state", "get_fcntl", "start_file_lock_holder"]
