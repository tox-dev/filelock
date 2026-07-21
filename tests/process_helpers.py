from __future__ import annotations

from contextlib import contextmanager
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from collections.abc import Generator
    from multiprocessing import Process

#: Reaping a process that has already been signaled is not a startup wait, and it has to stay under the suite's own
#: per-test timeout so the guard still reports the hang it was put there for.
_REAP_DEADLINE: Final[int] = 5


@contextmanager
def cleanup_processes(processes: list[Process]) -> Generator[None]:
    """Reap every process on the way out, whatever the block did."""
    try:
        yield
    finally:
        for process in processes:
            # An assertion can escape before every process starts, and terminating an unstarted one raises over the
            # failure that caused it, hiding the real error.
            if process.pid is not None:
                process.terminate()
                process.join(timeout=_REAP_DEADLINE)
                # A worker that outlives its test wedges the next one, and SIGTERM can be slow on a loaded runner.
                if process.is_alive():  # pragma: no cover  # terminate lands first unless the runner is saturated
                    process.kill()
                    process.join(timeout=_REAP_DEADLINE)
            # Release the multiprocessing finalizer here rather than leave it for a collection that lands in whichever
            # test is running by then, where it surfaces as an unraisable exception from a dead weakref callback.
            process.close()


__all__ = ["cleanup_processes"]
