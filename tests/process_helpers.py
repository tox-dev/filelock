from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from typing import TYPE_CHECKING, Final
from unittest.mock import patch

import filelock._soft
from filelock import SoftFileLock

if TYPE_CHECKING:
    from collections.abc import Callable, Generator
    from multiprocessing.process import BaseProcess
    from multiprocessing.synchronize import Event

#: Stays under the suite's per-test timeout so a wedged worker still surfaces as the hang it is.
_REAP_DEADLINE: Final[int] = 5


@contextmanager
def cleanup_processes(processes: list[BaseProcess]) -> Generator[None]:
    """Reap every process on the way out, whatever the block did."""
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
    """Break a stale marker, pausing right before the exchange and hanging right after it, until the test kills it."""
    exchange: Final = filelock._soft.exchange_names

    def exchange_then_hang(source: str, target: str) -> bool:
        ready.set()
        go.wait()
        exchange(source, target)
        exchanged.set()
        while True:
            time.sleep(_REAP_DEADLINE)

    with patch.object(filelock._soft, "exchange_names", new=exchange_then_hang):
        SoftFileLock(lock_file).acquire(blocking=False)


def call_within(call: Callable[[], object], seconds: float) -> tuple[bool, BaseException | None]:  # pragma: win32 cover
    """Run *call* on a daemon thread and report whether it returned within *seconds* and what it raised."""
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


__all__ = ["break_then_hang_after_exchange", "call_within", "cleanup_processes"]
