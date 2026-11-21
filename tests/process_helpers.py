from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from typing import TYPE_CHECKING, Final

import pytest

from filelock import SoftFileLock
from filelock._filesystem import exchange_names

if TYPE_CHECKING:
    from collections.abc import Callable, Generator
    from multiprocessing.process import BaseProcess
    from multiprocessing.synchronize import Event

#: Stays under the suite's per-test timeout so a wedged worker still surfaces as the hang it is.
_REAP_DEADLINE: Final[int] = 5


@contextmanager
def cleanup_processes(processes: list[BaseProcess]) -> Generator[None]:
    try:
        yield
    finally:
        for process in processes:
            # Terminating an unstarted process raises over the assertion that escaped before it started.
            if process.pid is not None:
                process.terminate()
                process.join(timeout=_REAP_DEADLINE)
                if process.is_alive():  # pragma: no cover  # SIGTERM only loses this race on a saturated runner
                    process.kill()
                    process.join(timeout=_REAP_DEADLINE)
            # Left open, the finalizer runs during whichever test the collector lands in, as an unraisable exception.
            process.close()


def break_then_hang_after_exchange(
    lock_file: str, ready: Event, go: Event, exchanged: Event
) -> None:  # pragma: win32 no cover
    def exchange_then_hang(source: str, target: str) -> bool:
        ready.set()
        go.wait()
        exchange_names(source, target)
        exchanged.set()
        while True:  # pragma: no cover  # the test kills this child, and the kill can land before this line is traced
            time.sleep(_REAP_DEADLINE)  # simplint: ignore[sleep-in-test]  # a blocked lock wait can swallow SIGTERM

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr("filelock._soft._exclusive._recovery.exchange_names", exchange_then_hang)
        SoftFileLock(lock_file).acquire(blocking=False)


def call_within(call: Callable[[], object], seconds: float) -> tuple[bool, BaseException | None]:  # pragma: win32 cover
    raised: list[BaseException] = []

    def run() -> None:
        try:
            call()
        except Exception as error:  # handed to the caller, which asserts on it
            raised.append(error)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    thread.join(seconds)
    return not thread.is_alive(), next(iter(raised), None)


__all__ = [
    "break_then_hang_after_exchange",
    "call_within",
    "cleanup_processes",
]
