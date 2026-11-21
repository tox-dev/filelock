from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    assert __spec__ is not None

__lazy_modules__ = [
    "inspect",
    "threading",
    "weakref",
    f"{__spec__.parent}._context",
    f"{__spec__.parent}._fork",
    f"{__spec__.parent}._shutdown",
]

import contextlib
import inspect
import os
import sys
from threading import RLock
from typing import TypeVar, cast
from weakref import WeakValueDictionary

from ._acquire import AcquireReturnProxy, LockAcquisition
from ._context import FileLockContext, InFlight, ThreadLocalFileContext
from ._fork import (
    register_fork_class,
    register_fork_object,
)
from ._settings import (
    UNSET_FILE_MODE,
    CloseErrorPolicy,
    ContextErrorPolicy,
    ExtraValue,
    LockSettings,
)
from ._shutdown import (
    LIVE_LOCKS,
    defer_singleton_release,
)
from ._singleton import FileLockMeta
from ._typing import override

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from types import FrameType
    from typing import Self

    from ._shutdown import FinalizerState

_SubclassValue = TypeVar("_SubclassValue")


class BaseFileLock(LockAcquisition, contextlib.ContextDecorator, metaclass=FileLockMeta):
    """Reentrant lock with acquisition and release hooks for backend implementations."""

    _finalizer_state: FinalizerState
    _instances: WeakValueDictionary[str, BaseFileLock]
    _instances_lock: RLock
    _instances_under_construction: set[str]

    # The async metaclass adds a frame between lifetime validation and the caller.
    _constructor_lifetime_warning_stacklevel: int = 3
    _preserve_lock_file_supported: bool = True
    _on_acquired_supported: bool = True

    @override
    def __init_subclass__(cls, **kwargs: _SubclassValue) -> None:
        """Give each lock subclass its own singleton registry and lock."""
        super().__init_subclass__(**kwargs)
        cls._instances = WeakValueDictionary()
        cls._instances_lock = RLock()
        cls._instances_under_construction = set()
        register_fork_class(cls)

    def __init__(  # ruff:ignore[too-many-arguments]  # public constructor: one parameter per documented lock option
        self,
        lock_file: str | os.PathLike[str],
        timeout: float = -1,
        mode: int = UNSET_FILE_MODE,
        thread_local: bool = True,  # ruff:ignore[boolean-type-hint-positional-argument, boolean-default-value-positional-argument]  # public API: positional bool kept for backwards compatibility
        *,
        blocking: bool = True,
        is_singleton: bool = False,
        poll_interval: float = 0.05,
        lifetime: float | None = None,
        context_error_policy: ContextErrorPolicy = "chain",
        close_error_policy: CloseErrorPolicy = "default",
        fallback_to_soft: bool = True,
        preserve_lock_file: bool = False,
        on_acquired: Callable[[int], None] | None = None,
    ) -> None:
        """
        :param lock_file: filesystem path used to coordinate holders
        :param timeout: default wait in seconds. A negative value or infinity waits indefinitely.
        :param mode: lock file permissions. When omitted, creation respects umask and default ACLs.
        :param thread_local: isolate holds and mutable options per thread, or share them across threads when false
        :param blocking: wait for a contended lock by default
        :param is_singleton: reuse the instance for the same path and compatible options
        :param poll_interval: default delay in seconds between attempts
        :param lifetime: break a soft lock after this many seconds, even if its owner still runs.
            Native locks warn and ignore this option.
        :param context_error_policy: chain a release failure to a body failure, or group both exceptions
        :param close_error_policy: propagate or suppress descriptor close errors, or use the backend default
        :param fallback_to_soft: allow Unix locks to use soft locking when the filesystem lacks flock
        :param preserve_lock_file: keep a native lock's pathname on release. This disables Unix soft fallback.
        :param on_acquired: callback receiving the borrowed native descriptor after each physical acquire.
            The callback may update the file, but must leave it locked and open. Raising rolls back acquisition.
        :raises TypeError: invalid option type
        :raises ValueError: invalid timeout, interval, permissions or combination of options
        """
        self._adopt_settings(
            LockSettings(
                thread_local=thread_local,
                is_singleton=is_singleton,
                context_error_policy=context_error_policy,
                close_error_policy=close_error_policy,
                fallback_to_soft=fallback_to_soft,
                preserve_lock_file=preserve_lock_file,
                on_acquired=on_acquired,
            )
        )

        self._context: FileLockContext = (ThreadLocalFileContext if thread_local else FileLockContext)(
            lock_file=os.fspath(lock_file),
            timeout=timeout,
            mode=mode,
            blocking=blocking,
            poll_interval=poll_interval,
            lifetime=lifetime,
        )
        register_fork_object(self)

    def _adopt_settings(self, settings: LockSettings) -> None:
        self._creator_pid = os.getpid()
        LIVE_LOCKS[id(self)] = self
        # A stand-in can finish the finalizer with a different id(self).
        self._holder_id = id(self)
        self._transition_lock = RLock()
        self._flockless_roots: frozenset[str] = frozenset()
        self._settings = settings
        self._thread_holds = {}
        self._thread_pending = {}

    def __enter__(self) -> Self:
        self.acquire()
        return self

    def acquire(
        self,
        timeout: float | None = None,
        poll_interval: float | None = None,
        *,
        poll_intervall: float | None = None,
        blocking: bool | None = None,
        cancel_check: Callable[[], bool] | None = None,
    ) -> AcquireReturnProxy[Self]:
        """
        Acquire the lock and return a context manager that releases it on exit.

        :param timeout: seconds to wait. A negative value waits indefinitely. ``None`` uses the instance default.
        :param poll_interval: seconds between attempts, or ``None`` for the instance default
        :param poll_intervall: deprecated spelling of *poll_interval*
        :param blocking: ``False`` makes one attempt. ``None`` uses the instance default.
        :param cancel_check: return ``True`` to stop a contended acquire
        :returns: a release-on-exit proxy. Entering the proxy does not acquire the lock again.
        :raises Timeout: the lock was unavailable at the deadline or the caller canceled
        :raises RuntimeError: acquisition would deadlock, the lock was inherited, or a failed unlock needs a retry
        :raises TypeError: *blocking* is not a boolean or a time value is not numeric
        :raises ValueError: a blocking timeout is NaN or the poll interval is invalid
        """
        self._raise_if_inherited()
        state = self._hold_state()
        wait = state.acquire_wait(timeout, poll_interval, poll_intervall, blocking=blocking, cancel_check=cancel_check)
        with self._transition_admission(wait):
            self._check_acquisition_state(state)
            canonical_path = self._resolve_lock_path()
            # A local reference to the record would create a cycle through this frame.
            depth = len(state.in_flight)
            previous_frame = state.acquiring
            levels_before: int | None = None
            try:  # ruff:ignore[too-many-statements-in-try-clause]  # each counted acquire step needs the same rollback
                state.in_flight.append(InFlight(frame=cast("FrameType", inspect.currentframe())))
                state.acquiring = state.in_flight[-1].frame
                levels_before = state.lock_counter if self.is_locked else 0
                self._acquire_level(state, levels_before, canonical_path, wait)
                proxy = AcquireReturnProxy(lock=self)
                state.acquiring = previous_frame
                del state.in_flight[depth:]
                return proxy  # ruff:ignore[try-consider-else]  # a signal at return must still roll back the hold
            except BaseException as error:
                try:
                    self._undo_in_flight_acquire(canonical_path, depth, levels_before, error)
                finally:
                    del state.in_flight[depth:]
                    state.acquiring = previous_frame
                raise

    def __del__(
        self,
        _getpid: Callable[[], int] = os.getpid,
        _is_finalizing: Callable[[], bool] = sys.is_finalizing,
        _suppress: type[contextlib.suppress] = contextlib.suppress,
    ) -> None:
        """Release abandoned holds when the last reference disappears."""
        # Bound at definition: module teardown at interpreter shutdown can set this module's globals to None first.
        if self.__dict__.get("_creator_pid") != _getpid():
            return  # pragma: forked child
        # CPython can still hand the cached singleton to a peer until it clears weak references.
        if (key := self._singleton_key) is not None and not _is_finalizing() and self._instances.get(key) is self:
            defer_singleton_release(self)
        else:
            # Teardown may also have cleared the globals the release path reads, here or in a subclass's module.
            with _suppress(Exception):
                self._release_abandoned_holds(exited_only=False)

    def _release_abandoned_holds(self, *, exited_only: bool) -> None:
        # A finalizer cannot report a cleanup failure to the caller who dropped the lock.
        with contextlib.suppress(Exception):
            self.release(force=True)
        # A dropped lock has no thread left inside it; at exit, a live daemon thread still is, so its hold stays.
        with contextlib.suppress(Exception):
            self._release_thread_holds(exited_only=exited_only)

    @classmethod
    @override
    def _reset_class_after_fork(cls) -> None:  # pragma: forked child
        cls._instances = WeakValueDictionary()
        cls._instances_lock = RLock()
        cls._instances_under_construction = set()

    def _singleton_extra_mismatches(  # ruff:ignore[no-self-use]  # the base class adds no options of its own
        self, _kwargs: Mapping[str, ExtraValue], /
    ) -> dict[str, tuple[str, str]]:
        # Only the subclass knows its extra options' defaults.
        return {}


__all__ = [
    "BaseFileLock",
]
