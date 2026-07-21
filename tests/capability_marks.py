"""Marks for runtime capabilities several test modules gate on.

Capabilities used by a single module stay private to it, as ``_NEEDS_LINK_DIR_FD`` and friends do. These span modules,
so they live here and read the same ``CAPABILITIES`` probes the coverage pragmas use. Gate on a probe rather than on an
interpreter or platform name: a name answers who is running, and every one of these questions is about what the runtime
can do.
"""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING

import pytest
from capabilities import CAPABILITIES

if TYPE_CHECKING:
    from typing import Final

#: The one gate here that asks who is running rather than what it can do. GraalPy's multiprocessing semaphores raise
#: ``OSError: [Errno 0] Success`` from ``_wait_semaphore.acquire(False)`` when ``Event.set()`` notifies a contended
#: condition, which fails whichever test happens to be coordinating processes at the time. The defect is in the
#: interpreter's own ``multiprocessing/synchronize.py``, it only appears on Linux under load, and an uncontended probe
#: cannot see it, so there is nothing to measure and no fix to make from here.
SKIP_ON_UNRELIABLE_PROCESS_SYNC: Final[pytest.MarkDecorator] = pytest.mark.skipif(
    sys.implementation.name == "graalpy",
    reason="this runtime raises OSError from multiprocessing Event.set() under contention",
)

NEEDS_FORK: Final[pytest.MarkDecorator] = pytest.mark.skipif(
    not CAPABILITIES["fork"], reason="installing fork handlers needs os.register_at_fork"
)

NEEDS_REGISTER_AT_FORK: Final[pytest.MarkDecorator] = pytest.mark.skipif(
    not CAPABILITIES["register-at-fork"], reason="the fork transition gate is installed through os.register_at_fork"
)

NEEDS_FORK1: Final[pytest.MarkDecorator] = pytest.mark.skipif(
    not CAPABILITIES["fork1"], reason="forking a single thread needs the Solaris os.fork1"
)

NEEDS_FCNTL: Final[pytest.MarkDecorator] = pytest.mark.skipif(
    not CAPABILITIES["fcntl"], reason="native flock semantics need the fcntl module"
)

NEEDS_SYMLINK: Final[pytest.MarkDecorator] = pytest.mark.skipif(
    not CAPABILITIES["symlink"], reason="creating a symlink needs a privilege this runtime does not grant"
)

#: Windows resolves a lock's parent with abspath, so a symlinked parent stays a distinct key and never collapses.
NEEDS_PARENT_SYMLINK_COLLAPSE: Final[pytest.MarkDecorator] = pytest.mark.skipif(
    not CAPABILITIES["symlink"] or sys.platform == "win32",
    reason="a symlinked parent collapses into one key only where the parent is resolved with realpath",
)

NEEDS_FILE_MODE: Final[pytest.MarkDecorator] = pytest.mark.skipif(
    not CAPABILITIES["file-mode"], reason="making a folder or a claim unreadable needs POSIX permission bits"
)

NEEDS_UNLINK_OPEN_FILE: Final[pytest.MarkDecorator] = pytest.mark.skipif(
    not CAPABILITIES["unlink-open-file"],
    reason="this runtime keeps an open marker undeletable, so no peer can take it from a live holder",
)

NEEDS_POSIX_SIGNALS: Final[pytest.MarkDecorator] = pytest.mark.skipif(
    not CAPABILITIES["posix-signals"], reason="liveness is probed with os.kill only where POSIX signals exist"
)

NEEDS_PROMPT_FINALIZATION: Final[pytest.MarkDecorator] = pytest.mark.skipif(
    not CAPABILITIES["prompt-finalization"],
    reason="a dropped reference does not run __del__ on a deferred collector",
)

NEEDS_COLLECTED_FINALIZATION: Final[pytest.MarkDecorator] = pytest.mark.skipif(
    not CAPABILITIES["collected-finalization"],
    reason="gc.collect() does not run __del__ on this runtime",
)

NEEDS_CLASS_COLLECTION: Final[pytest.MarkDecorator] = pytest.mark.skipif(
    not CAPABILITIES["class-collection"],
    reason="gc.collect() does not reclaim classes on this runtime",
)

NEEDS_GENERATOR_EXCEPTION_CONTEXT: Final[pytest.MarkDecorator] = pytest.mark.skipif(
    not CAPABILITIES["generator-exception-context"],
    reason="this runtime clears __context__ when an exception is thrown into a suspended frame",
)

NEEDS_AUDIT_EVENTS: Final[pytest.MarkDecorator] = pytest.mark.skipif(
    not CAPABILITIES["audit-events"],
    reason="this runtime never delivers audit events to an installed hook",
)

#: Raised from ``_GeneratorContextManagerBase.__aexit__``, so the lock's cancellation contract cannot be observed at
#: all there. Not strict: the deviation depends on where the cancellation lands, so some of these tests still pass.
XFAIL_WITHOUT_COROUTINE_CANCELLATION: Final[pytest.MarkDecorator] = pytest.mark.xfail(
    not CAPABILITIES["coroutine-cancellation"],
    reason="GraalPy's contextlib answers athrow() with RuntimeError instead of propagating the CancelledError",
    strict=False,
)

__all__ = [
    "NEEDS_AUDIT_EVENTS",
    "NEEDS_CLASS_COLLECTION",
    "NEEDS_COLLECTED_FINALIZATION",
    "NEEDS_FCNTL",
    "NEEDS_FILE_MODE",
    "NEEDS_FORK",
    "NEEDS_FORK1",
    "NEEDS_GENERATOR_EXCEPTION_CONTEXT",
    "NEEDS_PARENT_SYMLINK_COLLAPSE",
    "NEEDS_POSIX_SIGNALS",
    "NEEDS_PROMPT_FINALIZATION",
    "NEEDS_REGISTER_AT_FORK",
    "NEEDS_SYMLINK",
    "NEEDS_UNLINK_OPEN_FILE",
    "SKIP_ON_UNRELIABLE_PROCESS_SYNC",
    "XFAIL_WITHOUT_COROUTINE_CANCELLATION",
]
