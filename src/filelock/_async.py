"""Separate caller cancellation from backend task and executor-future results."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from importlib.machinery import ModuleSpec

__spec__ = cast("ModuleSpec", __spec__)

__lazy_modules__ = [
    "asyncio",
    "collections",
    "concurrent.futures",
    f"{__spec__.parent}._api",
    f"{__spec__.parent}._error",
]

import asyncio
import contextlib
import time
from collections import deque
from concurrent.futures import Future as ConcurrentFuture
from dataclasses import dataclass
from threading import Lock
from typing import TYPE_CHECKING, Final, Generic, Literal, NoReturn, TypeVar, cast
from weakref import WeakKeyDictionary

from ._api import (
    append_exception_context as _append_exception_context,
)
from ._api import (
    fork_transition as _fork_transition,
)
from ._api import (
    raise_chained_errors as _raise_chained_errors,
)
from ._api import (
    resolve_read_write_timeout as _resolve_read_write_timeout,
)
from ._error import Timeout

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Awaitable, Callable, Hashable
    from concurrent.futures import Executor

    from ._read_write import ReadWriteLock
    from ._soft_rw import SoftReadWriteLock

_T = TypeVar("_T")

#: The holder an async wrapper records its backend level under. No thread has this identifier, so sync code on the
#: executor thread that took the level cannot re-enter or release it.
TASKS_THREAD_ID: Final[int] = 0


@dataclass(frozen=True, slots=True, kw_only=True)
class BackendOutcome(Generic[_T]):
    value: _T | None = None
    error: BaseException | None = None


class AsyncTransitionGate:
    # First-in, first-out admission for the calls that transition one lock context.
    #
    # Each call queues a ticket, and the head of the queue holds the gate. A call that gives up removes its own ticket,
    # so abandoned calls do not pile up behind a long holder.
    def __init__(self) -> None:
        self._queue_lock: Final[Lock] = Lock()
        self._queue: Final[deque[ConcurrentFuture[None]]] = deque()

    @contextlib.asynccontextmanager
    async def hold(self) -> AsyncGenerator[None]:
        ticket: ConcurrentFuture[None] = ConcurrentFuture()
        try:
            self._join(ticket)
            if not ticket.done():
                await self._wait_for_turn(ticket, blocking=True, cancel_check=None, deadline=None, poll_interval=0.05)
            yield
        finally:
            self._leave(ticket)

    def _join(self, ticket: ConcurrentFuture[None]) -> None:
        with self._queue_lock:
            self._queue.append(ticket)
            if len(self._queue) == 1:
                ticket.set_result(None)

    async def _wait_for_turn(
        self,
        ticket: ConcurrentFuture[None],
        *,
        blocking: bool,
        cancel_check: Callable[[], bool] | None,
        deadline: float | None,
        poll_interval: float,
    ) -> None:
        if not blocking:
            raise AsyncTransitionUnavailableError
        waiter = asyncio.wrap_future(ticket)
        while not ticket.done():
            if cancel_check is not None and cancel_check():
                raise AsyncTransitionUnavailableError
            wait_interval = poll_interval
            if deadline is not None:
                if (remaining := deadline - time.perf_counter()) <= 0:
                    raise AsyncTransitionUnavailableError
                wait_interval = min(wait_interval, remaining)
            await asyncio.wait((waiter,), timeout=wait_interval)
            # A hand-off that a signal handler's exception cut short leaves the head undone, so the head settles itself.
            self._settle_head()

    def _settle_head(self) -> None:
        with self._queue_lock:
            self._settle_head_locked()

    def _settle_head_locked(self) -> None:
        if self._queue and not (head := self._queue[0]).done():
            head.set_result(None)

    def _leave(self, ticket: ConcurrentFuture[None]) -> None:
        with self._queue_lock:
            try:
                self._queue.remove(ticket)
            except ValueError:
                # The join never completed, or this ticket already left.
                return
            self._settle_head_locked()

    @contextlib.asynccontextmanager
    async def hold_for_acquire(
        self,
        *,
        blocking: bool,
        cancel_check: Callable[[], bool] | None,
        deadline: float | None,
        poll_interval: float,
    ) -> AsyncGenerator[None]:
        ticket: ConcurrentFuture[None] = ConcurrentFuture()
        try:
            self._join(ticket)
            if not ticket.done():
                await self._wait_for_turn(
                    ticket,
                    blocking=blocking,
                    cancel_check=cancel_check,
                    deadline=deadline,
                    poll_interval=poll_interval,
                )
            yield
        finally:
            self._leave(ticket)


class AsyncTransitionUnavailableError(Exception):
    pass


_TASK_OWNERS: Final[WeakKeyDictionary[ReadWriteLock | SoftReadWriteLock, _TaskOwners]] = WeakKeyDictionary()


_TASK_OWNERS_LOCK: Final[Lock] = Lock()


def task_owners_for(sync_lock: ReadWriteLock | SoftReadWriteLock) -> _TaskOwners:
    # Singleton wrappers share one sync lock, so they must share its task holds too.
    with _fork_transition(), _TASK_OWNERS_LOCK:
        if (owners := _TASK_OWNERS.get(sync_lock)) is None:
            owners = _TASK_OWNERS[sync_lock] = _TaskOwners()
        return owners


class _TaskOwners:
    # Per-task ownership in front of a sync reader/writer lock that tracks threads.
    #
    # An async wrapper runs each call on an executor thread, and that thread does not identify the calling task. Waiting
    # writers block new readers so a stream of readers cannot starve them. A thread lock guards the table because tasks
    # on different event loops share it. The table stands for one backend level, taken once for all tasks. Sync code can
    # end that level directly, with ``close()`` or a forced release on a shared singleton. The next admission then
    # forgets the holds instead of letting more tasks join them.
    def __init__(self) -> None:
        self._lock: Final[Lock] = Lock()
        self._mode: Literal["read", "write"] | None = None
        self._holds: dict[
            asyncio.Task[object], _TaskHold  # simplint: ignore[imprecise-annotation]  # task results vary
        ] = {}
        self._transitioning = False
        self._waiting_writers = 0
        #: One ticket per waiting call, settled on the next state change. A call that stops waiting takes its own out,
        #: so one that times out or is canceled leaves nothing behind.
        self._waiters: dict[ConcurrentFuture[None], None] = {}

    async def acquire(  # ruff:ignore[too-many-arguments]  # one keyword per piece of wrapper state the table consults
        self,
        mode: Literal["read", "write"],
        *,
        timeout: float,
        blocking: bool,
        lock_file: str,
        via: Hashable,
        enter: Callable[[float], Awaitable[None]],
        joinable: Callable[[], bool],
    ) -> None:
        task = _current_task()
        timeout = _resolve_read_write_timeout(timeout, blocking=blocking)
        deadline = None if not blocking or timeout < 0 else time.perf_counter() + timeout
        waiting_writer = False
        try:
            while isinstance(
                admission := self._admit(
                    task, mode, lock_file, via=via, joinable=joinable, waiting_writer=waiting_writer
                ),
                ConcurrentFuture,
            ):
                try:
                    if not blocking or (deadline is not None and time.perf_counter() >= deadline):
                        raise Timeout(lock_file)
                    if mode == "write" and not waiting_writer:
                        waiting_writer = True
                        with self._lock:
                            self._waiting_writers += 1
                    wait = None if deadline is None else deadline - time.perf_counter()
                    await asyncio.wait((asyncio.wrap_future(admission),), timeout=wait)
                finally:
                    self._forget(admission)
        finally:
            if waiting_writer:
                with self._lock:
                    self._waiting_writers -= 1
                    self._notify()
        if admission == "held":
            return
        try:
            await enter(-1 if deadline is None else max(0.0, deadline - time.perf_counter()))
        except BaseException:
            self._finish_transition(mode=None)
            raise
        self._finish_transition(mode=mode, holds={task: _TaskHold(depths={via: 1})})

    def _admit(  # ruff:ignore[too-many-arguments]  # the acquire's state, passed through under the table lock
        self,
        task: asyncio.Task[object],  # simplint: ignore[imprecise-annotation]  # task results vary
        mode: Literal["read", "write"],
        lock_file: str,
        *,
        via: Hashable,
        joinable: Callable[[], bool],
        waiting_writer: bool,
    ) -> Literal["held", "enter"] | ConcurrentFuture[None]:
        with self._lock:
            # A "held" answer trusts the backend level. Once the backend lost it, the holds are gone and the next task
            # has to take the lock again.
            if self._mode is not None and not self._transitioning and not joinable():
                self._holds.clear()
                self._mode = None
            if (hold := self._holds.get(task)) is not None:
                if self._mode != mode:
                    msg = (
                        f"Cannot acquire {mode} lock on {lock_file}: already holding a "
                        f"{'write' if mode == 'read' else 'read'} lock "
                        f"({'downgrade' if mode == 'read' else 'upgrade'} not allowed)"
                    )
                    raise RuntimeError(msg)
                hold.depths[via] = hold.depths.get(via, 0) + 1
                return "held"
            writers_ahead = self._waiting_writers - waiting_writer
            if not self._transitioning:
                if mode == "read" and self._mode == "read" and not writers_ahead:
                    self._holds[task] = _TaskHold(depths={via: 1})
                    return "held"
                if not self._holds and (mode == "write" or not writers_ahead):
                    self._transitioning, self._mode = True, mode
                    return "enter"
            return self._ticket()

    def _ticket(self) -> ConcurrentFuture[None]:
        # Called under the table lock, in the section that decided to wait, so no state change slips in between.
        ticket: ConcurrentFuture[None] = ConcurrentFuture()
        self._waiters[ticket] = None
        return ticket

    def _forget(self, ticket: ConcurrentFuture[None]) -> None:
        with self._lock:
            self._waiters.pop(ticket, None)

    def _notify(self) -> None:
        waiters, self._waiters = self._waiters, {}
        for ticket in waiters:
            ticket.set_result(None)

    def _finish_transition(
        self,
        *,
        mode: Literal["read", "write"] | None,
        holds: dict[asyncio.Task[object], _TaskHold]  # simplint: ignore[imprecise-annotation]  # task results vary
        | None = None,
    ) -> None:
        with self._lock:
            self._transitioning, self._mode = False, mode
            if mode is not None and holds:
                for task, hold in holds.items():
                    self._holds.setdefault(task, _TaskHold(depths={})).depths.update(hold.depths)
            self._notify()

    async def release(
        self,
        *,
        force: bool,
        lock_file: str,
        via: Hashable,
        leave: Callable[[], asyncio.Future[BackendOutcome[None]]],
        still_held: Callable[[], bool],
    ) -> None:
        task = _current_task()
        with self._lock:
            if (hold := self._holds.get(task)) is None or via not in hold.depths:
                if force:
                    return
                msg = f"Cannot release a lock on {lock_file} that is not held by this task through this wrapper"
                raise RuntimeError(msg)
            if not force and hold.depths[via] > 1:
                hold.depths[via] -= 1
                return
            removed = hold.depths.pop(via)
            if hold.depths:
                return
            del self._holds[task]
            if self._holds:
                return
            self._transitioning = True
            mode = self._mode
        if not still_held():
            self._finish_transition(mode=None)
            msg = f"Cannot release a lock on {lock_file} that is not held by this task"
            raise RuntimeError(msg)
        try:
            cancellation = await _drain_cancellation(leave())
        except BaseException:
            hold.depths[via] = removed
            # Keep the hold for a retried release only while the sync lock still has the level. Restored after the sync
            # lock let go, it would stand for a lock nobody holds.
            self._finish_transition(mode=mode if still_held() else None, holds={task: hold})
            raise
        self._finish_transition(mode=None)
        if cancellation is not None:
            # The release succeeded, so a restored hold would be a phantom no backend call can clear.
            raise cancellation

    async def retire(
        self,
        via: Hashable | None,
        *,
        leave: Callable[[], asyncio.Future[BackendOutcome[None]]],
        still_held: Callable[[], bool],
    ) -> None:
        # End the holds taken through *via*, or every hold when it is ``None``, as one transition.
        #
        # Leaves the backend through *leave* when that ends the last hold, and always when *via* is ``None``. Arrivals
        # wait while it runs, so none joins a hold the backend is letting go of. A transition already under way is
        # waited
        # out first: a task entering through *via* must not get a hold after the wrapper is gone. A cancellation is
        # raised once the holds are settled.
        cancellation: asyncio.CancelledError | None = None
        while isinstance(settled := self._drop_holds_via(via), ConcurrentFuture):
            try:
                await wait_until_done(asyncio.wrap_future(settled))
            except asyncio.CancelledError as error:
                cancellation = error
            finally:
                self._forget(settled)
        mode, dropped = settled
        if via is None or (mode is not None and still_held()):
            try:
                cancellation = await _drain_cancellation(leave()) or cancellation
            except BaseException:
                # A close that failed with the backend still holding stays retryable, as a failed release does.
                self._finish_transition(mode=mode if still_held() else None, holds=dropped)
                raise
            self._finish_transition(mode=None)
        elif mode is not None:
            # Sync code already ended the level taken for tasks, so there is nothing to give back.
            self._finish_transition(mode=None)
        if cancellation is not None:
            raise cancellation

    def _drop_holds_via(
        self, via: Hashable | None
    ) -> (
        tuple[
            Literal["read", "write"] | None,
            dict[asyncio.Task[object], _TaskHold],  # simplint: ignore[imprecise-annotation]  # task results vary
        ]
        | ConcurrentFuture[None]
    ):
        # Returns the future a running transition settles, or else the mode to leave (None when other holds keep the
        # backend level) and the dropped holds. The table stays transitioning while that mode is left.
        with self._lock:
            if self._transitioning:
                return self._ticket()
            if via is None:
                dropped = dict(self._holds)
                self._holds.clear()
            else:
                dropped = {}
                for task, hold in list(self._holds.items()):
                    if (count := hold.depths.pop(via, None)) is not None:
                        dropped[task] = _TaskHold(depths={via: count})
                        if not hold.depths:
                            del self._holds[task]
            if self._holds and via is not None:
                return None, dropped
            mode = self._mode
            self._transitioning = mode is not None or via is None
            return mode, dropped


def _current_task() -> asyncio.Task[object]:  # simplint: ignore[imprecise-annotation]  # task results vary
    if (task := asyncio.current_task()) is None:
        msg = "an async reader/writer lock must be used from inside an asyncio task"
        raise RuntimeError(msg)
    return task


@dataclass(frozen=True, slots=True, kw_only=True)
class _TaskHold:
    # One task's hold, as the nesting count per wrapper it entered through.
    #
    # A task's hold ends when its last wrapper level is released or closed. Closing one wrapper ends only the levels
    # taken through it.
    depths: dict[Hashable, int]


async def _drain_cancellation(future: asyncio.Future[BackendOutcome[None]]) -> asyncio.CancelledError | None:
    # Returned, not raised, so the caller can tell a call that completed under cancellation from one that failed.
    try:
        await wait_until_done(future)
    except asyncio.CancelledError as cancellation:
        try:
            await drain_future(future)
        except BaseException as error:  # ruff:ignore[blind-except]  # reported with the cancellation below
            raise_cancelled_error(cancellation, error)
        return cancellation
    future_result(future)
    return None


async def drain_future(future: asyncio.Future[BackendOutcome[_T]]) -> _T:
    while not future.done():
        with contextlib.suppress(asyncio.CancelledError):
            await wait_until_done(future)
    return future_result(future)


def raise_cancelled_error(cancellation: asyncio.CancelledError, error: BaseException) -> NoReturn:
    # A reconciliation step failed while unwinding a cancellation, so keep both exception chains. Splice the error's
    # existing context onto the cancellation, then make the cancellation the error's context, so both the failure and
    # the cancellation that triggered it survive. Shared by the async wrappers so cancellations report the same way.
    if (context := error.__context__) is not None and context is not cancellation:
        if (cancellation_context := cancellation.__context__) is not None:
            _append_exception_context(context, cancellation_context)
        cancellation.__context__ = context
    error.__context__ = cancellation
    _raise_chained_errors(error)


def future_result(future: asyncio.Future[BackendOutcome[_T]]) -> _T:
    outcome = future.result()
    if (error := outcome.error) is None:
        return cast("_T", outcome.value)
    context = error.__context__
    try:
        raise error  # ruff:ignore[raise-within-try]  # the handler restores context changed across the async boundary
    except BaseException:
        error.__context__ = context
        raise


async def wait_until_done(future: asyncio.Future[_T]) -> None:
    if not future.done():
        await asyncio.wait((future,))


def run_in_executor(
    loop: asyncio.AbstractEventLoop, executor: Executor | None, func: Callable[[], _T]
) -> asyncio.Future[BackendOutcome[_T]]:
    outcome: asyncio.Future[BackendOutcome[_T]] = loop.create_future()

    def settle(call: asyncio.Future[BackendOutcome[_T]]) -> None:
        try:
            result = call.result()
        except asyncio.CancelledError:
            # shutdown(cancel_futures=True) dropped the call unrun: a CancelledError would cancel a task nobody canceled
            # and hide that the lock state never changed.
            result = BackendOutcome(error=RuntimeError("the executor shut down before running the lock backend call"))
        outcome.set_result(result)

    loop.run_in_executor(executor, capture_call, func).add_done_callback(settle)
    return outcome


def capture_call(func: Callable[[], _T]) -> BackendOutcome[_T]:
    try:
        return BackendOutcome(value=func())
    except BaseException as error:  # ruff:ignore[blind-except]  # backend control-flow exceptions are operation results
        return BackendOutcome(error=error)


async def capture_awaitable(awaitable: Awaitable[_T]) -> BackendOutcome[_T]:
    try:
        return BackendOutcome(value=await awaitable)
    except BaseException as error:  # ruff:ignore[blind-except]  # backend cancellation must remain distinct from caller cancellation
        return BackendOutcome(error=error)


__all__ = [
    "TASKS_THREAD_ID",
    "AsyncTransitionGate",
    "AsyncTransitionUnavailableError",
    "BackendOutcome",
    "capture_awaitable",
    "capture_call",
    "drain_future",
    "future_result",
    "raise_cancelled_error",
    "run_in_executor",
    "task_owners_for",
    "wait_until_done",
]
