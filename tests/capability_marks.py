"""Marks for runtime capabilities several test modules gate on.

Capabilities used by a single module stay private to it, as ``_NEEDS_SYMLINK`` and friends do. These span modules, so
they live here and read the same ``CAPABILITIES`` probes the coverage pragmas use.
"""

from __future__ import annotations

import pytest
from coverage_pragmas import CAPABILITIES

#: A dropped reference releases what its __del__ releases. Only a refcounting collector does this.
NEEDS_PROMPT_FINALIZATION = pytest.mark.skipif(
    not CAPABILITIES["prompt-finalization"],
    reason="a dropped reference does not run __del__ on a deferred collector",
)

#: gc.collect() runs pending __del__ methods. GraalPy hands them to the host collector and never gets them back.
NEEDS_COLLECTED_FINALIZATION = pytest.mark.skipif(
    not CAPABILITIES["collected-finalization"],
    reason="gc.collect() does not run __del__ on this runtime",
)

#: gc.collect() reclaims a dynamically built class once its last reference goes.
NEEDS_CLASS_COLLECTION = pytest.mark.skipif(
    not CAPABILITIES["class-collection"],
    reason="gc.collect() does not reclaim classes on this runtime",
)

#: An exception thrown into a suspended generator or coroutine keeps its __context__.
NEEDS_GENERATOR_EXCEPTION_CONTEXT = pytest.mark.skipif(
    not CAPABILITIES["generator-exception-context"],
    reason="this runtime clears __context__ when an exception is thrown into a suspended frame",
)

#: sys.audit delivers to hooks installed with sys.addaudithook. GraalPy accepts the hook and never calls it.
NEEDS_AUDIT_EVENTS = pytest.mark.skipif(
    not CAPABILITIES["audit-events"],
    reason="this runtime never delivers audit events to an installed hook",
)

#: A cancellation crossing an async context manager surfaces as CancelledError rather than the interpreter's own
#: bookkeeping error. GraalPy's contextlib raises ``RuntimeError: generator didn't stop after athrow()`` from
#: ``_GeneratorContextManagerBase.__aexit__`` instead, so the lock's cancellation contract cannot be observed there.
#: Not strict: the deviation depends on where the cancellation lands, so some of these tests still pass.
XFAIL_WITHOUT_COROUTINE_CANCELLATION = pytest.mark.xfail(
    not CAPABILITIES["coroutine-cancellation"],
    reason="GraalPy's contextlib answers athrow() with RuntimeError instead of propagating the CancelledError",
    strict=False,
)

__all__ = [
    "NEEDS_AUDIT_EVENTS",
    "NEEDS_CLASS_COLLECTION",
    "NEEDS_COLLECTED_FINALIZATION",
    "NEEDS_GENERATOR_EXCEPTION_CONTEXT",
    "NEEDS_PROMPT_FINALIZATION",
    "XFAIL_WITHOUT_COROUTINE_CANCELLATION",
]
