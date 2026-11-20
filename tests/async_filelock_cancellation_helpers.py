from __future__ import annotations

import importlib
import multiprocessing
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING, NamedTuple, ParamSpec, TypeVar, cast

from filelock import FileLock, Timeout

if TYPE_CHECKING:
    import asyncio
    from collections.abc import Callable
    from concurrent.futures import Future
    from multiprocessing.sharedctypes import Synchronized
    from typing import Protocol

    class FcntlModule(Protocol):
        LOCK_UN: int

        @staticmethod
        def flock(fd: int, operation: int) -> None: ...


_P = ParamSpec("_P")
_T = TypeVar("_T")


def assert_cancellation_message(error: asyncio.CancelledError, message: str) -> None:
    assert error.args == (message,)


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
        assert (probe.exitcode, acquired.value) == (0, available)
    finally:
        if probe.is_alive():  # pragma: no cover - cleanup for a hung child after the assertion fails
            probe.terminate()
            probe.join(timeout=5)
        probe.close()


def _probe_file_lock(lock_file: str, acquired: Synchronized[bool]) -> None:
    try:
        with FileLock(lock_file, timeout=0):
            acquired.value = True
    except Timeout:  # pragma: win32 no cover
        pass  # pragma: win32 no cover


def start_file_lock_holder(lock_file: str) -> FileLockHolder:
    holder_started = threading.Event()
    finish_holder = threading.Event()
    holder = threading.Thread(target=_hold_file_lock, args=(lock_file, holder_started, finish_holder))
    holder.start()
    return FileLockHolder(holder, holder_started, finish_holder)


def _hold_file_lock(lock_file: str, holder_started: threading.Event, finish_holder: threading.Event) -> None:
    with FileLock(lock_file):
        holder_started.set()
        assert finish_holder.wait(timeout=5)


class FileLockHolder(NamedTuple):
    thread: threading.Thread
    holder_started: threading.Event
    finish_holder: threading.Event


class FailingSubmitExecutor(ThreadPoolExecutor):
    # Fails one submission after the pool queued the call, as a worker thread that cannot start does.
    def __init__(self, *, after_call_ran: bool) -> None:
        super().__init__(max_workers=1)
        self.fail_next_submit = False
        self._after_call_ran = after_call_ran

    def submit(self, fn: Callable[_P, _T], /, *args: _P.args, **kwargs: _P.kwargs) -> Future[_T]:
        future = super().submit(fn, *args, **kwargs)
        if not self.fail_next_submit:
            return future
        self.fail_next_submit = False
        if self._after_call_ran:
            future.result(timeout=5)
        msg = "can't start new thread"
        raise RuntimeError(msg)


__all__ = [
    "FailingSubmitExecutor",
    "FileLockHolder",
    "assert_cancellation_message",
    "assert_file_lock_state",
    "get_fcntl",
    "start_file_lock_holder",
]
