"""Separate caller cancellation from backend task and executor-future results."""

from __future__ import annotations

import asyncio
import contextlib
import time
from concurrent.futures import Future as ConcurrentFuture
from dataclasses import dataclass
from threading import Lock
from typing import TYPE_CHECKING, Final, Generic, Literal, NoReturn, TypeVar, cast
from weakref import WeakKeyDictionary

from ._api import _append_exception_context, _fork_transition, _raise_chained_errors
from ._error import Timeout

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable

    from ._read_write import ReadWriteLock
    from ._soft_rw import SoftReadWriteLock

_T = TypeVar("_T")


class _AsyncTransitionUnavailableError(Exception):
    pass


@dataclass(frozen=True)
class _BackendOutcome(Generic[_T]):
    value: _T | None = None
    error: BaseException | None = None


class _AsyncTransitionGate:
    def __init__(self) -> None:
        self._tail_lock: Final[Lock] = Lock()
        self._tail: ConcurrentFuture[None] | None = None

    @contextlib.asynccontextmanager
    async def hold(self) -> AsyncIterator[None]:
        ticket: ConcurrentFuture[None] = ConcurrentFuture()
        with self._tail_lock:
            predecessor = self._tail
            self._tail = ticket
        if predecessor is not None:
            try:
                await _wait_until_done(asyncio.wrap_future(predecessor))
            except asyncio.CancelledError:
                predecessor.add_done_callback(lambda _predecessor: self._leave(ticket))
                raise
        try:
            yield
        finally:
            self._leave(ticket)

    @contextlib.asynccontextmanager
    async def hold_for_acquire(
        self,
        *,
        blocking: bool,
        cancel_check: Callable[[], bool] | None,
        deadline: float | None,
        poll_interval: float,
    ) -> AsyncIterator[None]:
        ticket: ConcurrentFuture[None] = ConcurrentFuture()
        with self._tail_lock:
            predecessor = self._tail
            self._tail = ticket
        if predecessor is not None and not predecessor.done():
            try:
                await self._wait_for_predecessor(
                    predecessor,
                    blocking=blocking,
                    cancel_check=cancel_check,
                    deadline=deadline,
                    poll_interval=poll_interval,
                )
            except BaseException:
                predecessor.add_done_callback(lambda _predecessor: self._leave(ticket))
                raise
        try:
            yield
        finally:
            self._leave(ticket)

    @staticmethod
    async def _wait_for_predecessor(
        predecessor: ConcurrentFuture[None],
        *,
        blocking: bool,
        cancel_check: Callable[[], bool] | None,
        deadline: float | None,
        poll_interval: float,
    ) -> None:
        if not blocking:
            raise _AsyncTransitionUnavailableError
        waiter = asyncio.wrap_future(predecessor)
        while not predecessor.done():
            if cancel_check is not None and cancel_check():
                raise _AsyncTransitionUnavailableError
            if deadline is not None:
                if (remaining := deadline - time.perf_counter()) <= 0:
                    raise _AsyncTransitionUnavailableError
                wait_interval = min(poll_interval, remaining) if cancel_check is not None else remaining
            else:
                wait_interval = poll_interval if cancel_check is not None else None
            await asyncio.wait((waiter,), timeout=wait_interval)

    def _leave(self, ticket: ConcurrentFuture[None]) -> None:
        with self._tail_lock:
            if self._tail is ticket:
                self._tail = None
        ticket.set_result(None)


class _TaskOwners:
    """
    Per-task ownership in front of a sync reader/writer lock that tracks threads.

    An async wrapper runs each call on an executor thread, and that thread does not identify the calling task. Waiting
    writers block new readers so a stream of readers cannot starve them. A thread lock guards the table because tasks on
    different event loops share it.
    """

    def __init__(self) -> None:
        self._lock: Final[Lock] = Lock()
        self._mode: Literal["read", "write"] | None = None
        self._depths: dict[asyncio.Task[object], int] = {}
        self._transitioning = False
        self._waiting_writers = 0
        self._changed: ConcurrentFuture[None] = ConcurrentFuture()

    async def acquire(
        self,
        mode: Literal["read", "write"],
        *,
        timeout: float,
        blocking: bool,
        lock_file: str,
        enter: Callable[[float], Awaitable[None]],
    ) -> None:
        task = _current_task()
        deadline = None if timeout < 0 else time.perf_counter() + timeout
        waiting_writer = False
        try:
            while isinstance(
                admission := self._admit(task, mode, lock_file, waiting_writer=waiting_writer), ConcurrentFuture
            ):
                if not blocking or (deadline is not None and time.perf_counter() >= deadline):
                    raise Timeout(lock_file)
                if mode == "write" and not waiting_writer:
                    waiting_writer = True
                    with self._lock:
                        self._waiting_writers += 1
                wait = None if deadline is None else deadline - time.perf_counter()
                await asyncio.wait((asyncio.wrap_future(admission),), timeout=wait)
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
        self._finish_transition(mode=mode, holder=task)

    def _admit(
        self,
        task: asyncio.Task[object],
        mode: Literal["read", "write"],
        lock_file: str,
        *,
        waiting_writer: bool,
    ) -> Literal["held", "enter"] | ConcurrentFuture[None]:
        with self._lock:
            if (depth := self._depths.get(task)) is not None:
                if self._mode != mode:
                    msg = (
                        f"Cannot acquire {mode} lock on {lock_file}: already holding a "
                        f"{'write' if mode == 'read' else 'read'} lock "
                        f"({'downgrade' if mode == 'read' else 'upgrade'} not allowed)"
                    )
                    raise RuntimeError(msg)
                self._depths[task] = depth + 1
                return "held"
            writers_ahead = self._waiting_writers - waiting_writer
            if not self._transitioning:
                if mode == "read" and self._mode == "read" and not writers_ahead:
                    self._depths[task] = 1
                    return "held"
                if not self._depths and (mode == "write" or not writers_ahead):
                    self._transitioning, self._mode = True, mode
                    return "enter"
            return self._changed

    async def release(self, *, force: bool, lock_file: str, leave: Callable[[], Awaitable[None]]) -> None:
        task = _current_task()
        with self._lock:
            if (depth := self._depths.get(task)) is None:
                if force:
                    return
                msg = f"Cannot release a lock on {lock_file} that is not held by this task"
                raise RuntimeError(msg)
            if not force and depth > 1:
                self._depths[task] = depth - 1
                return
            del self._depths[task]
            if self._depths:
                return
            self._transitioning = True
            mode = self._mode
        try:
            await leave()
        except BaseException:
            # The sync lock may still hold the transaction, so keep the hold for a retried release to find.
            self._finish_transition(mode=mode, holder=task)
            raise
        self._finish_transition(mode=None)

    def reset(self) -> None:
        with self._lock:
            self._depths.clear()
            self._mode, self._transitioning = None, False
            self._notify()

    def _finish_transition(
        self, *, mode: Literal["read", "write"] | None, holder: asyncio.Task[object] | None = None
    ) -> None:
        with self._lock:
            self._transitioning, self._mode = False, mode
            if holder is not None:
                self._depths[holder] = 1
            self._notify()

    def _notify(self) -> None:
        changed, self._changed = self._changed, ConcurrentFuture()
        changed.set_result(None)


_TASK_OWNERS: Final[WeakKeyDictionary[ReadWriteLock | SoftReadWriteLock, _TaskOwners]] = WeakKeyDictionary()
_TASK_OWNERS_LOCK: Final[Lock] = Lock()


def _task_owners_for(sync_lock: ReadWriteLock | SoftReadWriteLock) -> _TaskOwners:
    """Singleton wrappers share one sync lock, so they must share its task holds too."""
    with _fork_transition(), _TASK_OWNERS_LOCK:
        if (owners := _TASK_OWNERS.get(sync_lock)) is None:
            owners = _TASK_OWNERS[sync_lock] = _TaskOwners()
        return owners


def _current_task() -> asyncio.Task[object]:
    if (task := asyncio.current_task()) is None:
        msg = "an async reader/writer lock must be used from inside an asyncio task"
        raise RuntimeError(msg)
    return task


async def _drain_future(future: asyncio.Future[_BackendOutcome[_T]]) -> _T:
    while not future.done():
        with contextlib.suppress(asyncio.CancelledError):
            await _wait_until_done(future)
    return _future_result(future)


async def _wait_until_done(future: asyncio.Future[_T]) -> None:
    if not future.done():
        await asyncio.wait((future,))


def _future_result(future: asyncio.Future[_BackendOutcome[_T]]) -> _T:
    outcome = future.result()
    if (error := outcome.error) is None:
        return cast("_T", outcome.value)
    context = error.__context__
    try:
        raise error  # ruff:ignore[raise-within-try]  # the handler restores context changed across the async boundary
    except BaseException:
        error.__context__ = context
        raise


def _capture_call(func: Callable[[], _T]) -> _BackendOutcome[_T]:
    try:
        return _BackendOutcome(value=func())
    except BaseException as error:  # ruff:ignore[blind-except]  # backend control-flow exceptions are operation results
        return _BackendOutcome(error=error)


def _raise_cancelled_error(cancellation: asyncio.CancelledError, error: BaseException) -> NoReturn:
    # A reconciliation step failed while unwinding a cancellation, so keep both exception chains. Splice the error's
    # existing context onto the cancellation, then make the cancellation the error's context, so both the failure and
    # the cancellation that triggered it survive. Shared by the async wrappers so cancellations report the same way.
    if (context := error.__context__) is not None and context is not cancellation:
        if (cancellation_context := cancellation.__context__) is not None:
            _append_exception_context(context, cancellation_context)
        cancellation.__context__ = context
    error.__context__ = cancellation
    _raise_chained_errors(error)


async def _capture_awaitable(awaitable: Awaitable[_T]) -> _BackendOutcome[_T]:
    try:
        return _BackendOutcome(value=await awaitable)
    except BaseException as error:  # ruff:ignore[blind-except]  # backend cancellation must remain distinct from caller cancellation
        return _BackendOutcome(error=error)


__all__ = [
    "_AsyncTransitionGate",
    "_AsyncTransitionUnavailableError",
    "_BackendOutcome",
    "_capture_awaitable",
    "_capture_call",
    "_drain_future",
    "_future_result",
    "_raise_cancelled_error",
    "_task_owners_for",
    "_wait_until_done",
]
