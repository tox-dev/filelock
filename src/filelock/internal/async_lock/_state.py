from __future__ import annotations

from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from importlib.machinery import ModuleSpec

__spec__ = cast("ModuleSpec", __spec__)

__lazy_modules__ = [f"{__spec__.parent}._transitions"]

from dataclasses import dataclass, field, fields
from typing import TYPE_CHECKING

from filelock.internal.core import FileLockContext, ThreadLocalFileContext, delegate_to_current

from ._transitions import AsyncTransitionGate

if TYPE_CHECKING:
    import asyncio
    from concurrent import futures


def _new_transition_gate() -> AsyncTransitionGate:
    # Called per context rather than passed as the factory, so the lazy import resolves on first use, not at import.
    return AsyncTransitionGate()


@dataclass  # simplint: ignore[dataclass-frozen, dataclass-slots, dataclass-kw-only]  # established mutable context
class AsyncFileLockContext(FileLockContext):
    """
    Keep executor settings and pending transitions with each async hold.

    :ivar run_in_executor: whether synchronous backend calls use a worker
    :ivar executor: worker pool, or None for the event loop default
    :ivar loop: fallback loop for finalization
    :ivar releasing_through_backend: whether a backend release is in progress
    :ivar transition_gate: queue for competing acquire and release calls
    :ivar rollback_tried: whether cleanup already attempted to undo an acquire

    """

    run_in_executor: bool = True
    executor: futures.Executor | None = None
    loop: asyncio.AbstractEventLoop | None = None

    releasing_through_backend: bool = False

    # Thread-local contexts need separate gates so one thread can release while another waits.
    transition_gate: AsyncTransitionGate = field(default_factory=_new_transition_gate)
    rollback_tried: bool = field(default=False, kw_only=True)


class AsyncThreadLocalFileContext(AsyncFileLockContext, ThreadLocalFileContext):
    """
    Give each thread its own async hold and transition gate.

    :param lock_file: lock path
    :param timeout: seconds to wait, negative for unlimited
    :param mode: file creation permissions
    :param blocking: whether to wait for a contended lock
    :param poll_interval: seconds between acquisition attempts
    :param lifetime: soft marker expiry in seconds
    :param run_in_executor: whether backend calls use a worker
    :param executor: worker pool, or None for the loop default
    :param loop: fallback loop for finalization
    :ivar current: calling thread's context

    """

    # threading.local runs __init__ again with these arguments the first time each thread touches the context.
    def __init__(  # ruff:ignore[too-many-arguments]  # mirrors the AsyncFileLockContext fields set at construction
        self,
        *,
        lock_file: str,
        timeout: float,
        mode: int,
        blocking: bool,
        poll_interval: float,
        lifetime: float | None = None,
        run_in_executor: bool = True,
        executor: futures.Executor | None = None,
        loop: asyncio.AbstractEventLoop | None = None,
    ) -> None:
        self.current = AsyncFileLockContext(
            lock_file=lock_file,
            timeout=timeout,
            mode=mode,
            blocking=blocking,
            poll_interval=poll_interval,
            lifetime=lifetime,
            run_in_executor=run_in_executor,
            executor=executor,
            loop=loop,
        )


# Dataclass annotation lookup during class creation needs the defining module on Python 3.11.
AsyncFileLockContext.__module__ = AsyncThreadLocalFileContext.__module__ = "filelock.asyncio"

for _field in fields(AsyncFileLockContext):
    setattr(AsyncThreadLocalFileContext, _field.name, delegate_to_current(_field.name))


__all__ = [
    "AsyncFileLockContext",
    "AsyncThreadLocalFileContext",
]
