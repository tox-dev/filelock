from __future__ import annotations

from contextlib import contextmanager
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from collections.abc import Generator
    from multiprocessing.process import BaseProcess

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


__all__ = ["cleanup_processes"]
