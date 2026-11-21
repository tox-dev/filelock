from __future__ import annotations

from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from importlib.machinery import ModuleSpec

__spec__ = cast("ModuleSpec", __spec__)

__lazy_modules__ = ["functools", "threading", "weakref", f"{__spec__.parent}._heartbeat"]

import os
import threading
from functools import partial
from typing import TYPE_CHECKING
from weakref import WeakValueDictionary

from filelock.internal.core import (
    canonical,
    check_blocking,
    ensure_current_process,
    override,
    path_key,
    seconds,
    timeout_behavior,
)

from ._heartbeat import validate_intervals

if TYPE_CHECKING:
    from collections.abc import Callable

    from filelock import LeaseCompromise

    from ._lock import SoftReadWriteLock


class SoftRWMeta(type):
    _instances: WeakValueDictionary[str, SoftReadWriteLock]
    _instances_lock: threading.RLock
    _instances_under_construction: set[str]

    @override
    def __call__(  # ruff:ignore[too-many-arguments]  # forwards the public constructor's documented parameters
        cls,
        lock_file: str | os.PathLike[str],
        timeout: float = -1,
        *,
        blocking: bool = True,
        is_singleton: bool = True,
        heartbeat_interval: float = 30.0,
        stale_threshold: float | None = None,
        poll_interval: float = 0.25,
        on_compromise: Callable[[LeaseCompromise], None] | None = None,
    ) -> SoftReadWriteLock:
        ensure_current_process()
        check_blocking(blocking)
        timeout = seconds("timeout", timeout)
        # Older subclasses may omit on_compromise from their constructors.
        create = partial(
            super().__call__,
            lock_file,
            timeout,
            blocking=blocking,
            is_singleton=is_singleton,
            heartbeat_interval=heartbeat_interval,
            stale_threshold=stale_threshold,
            poll_interval=poll_interval,
            **({} if on_compromise is None else {"on_compromise": on_compromise}),
        )
        if not is_singleton:
            return create()

        normalized = path_key(canonical(lock_file))
        with cls._instances_lock:
            if (instance := cls._instances.get(normalized)) is None:
                return cls._create_singleton(normalized, lock_file, create)
            if (
                timeout_behavior(instance.timeout, any_negative=False) != timeout_behavior(timeout, any_negative=False)
                or instance.blocking != blocking
            ):
                msg = (
                    f"Singleton lock created with timeout={instance.timeout}, blocking={instance.blocking},"
                    f" cannot be changed to timeout={timeout}, blocking={blocking}"
                )
                raise ValueError(msg)
            if on_compromise is not None and instance._losses.callback != on_compromise:  # ruff: ignore[private-member-access]  # the metaclass owns the singleton it compares
                msg = f"Singleton lock created with a different on_compromise callback for {lock_file!s}"
                raise ValueError(msg)
            validate_intervals(heartbeat_interval, stale_threshold, poll_interval)
            return instance

    def _create_singleton(
        cls, normalized: str, lock_file: str | os.PathLike[str], create: Callable[[], SoftReadWriteLock]
    ) -> SoftReadWriteLock:
        registry = cls._instances_under_construction
        if normalized in registry:
            msg = f"Singleton lock construction is already active for {lock_file!s}"
            raise RuntimeError(msg)
        construction_pid = os.getpid()
        registry.add(normalized)
        try:
            instance = create()
        finally:
            registry.discard(normalized)
        if os.getpid() != construction_pid:  # pragma: needs fork
            msg = "Lock construction cannot continue after fork; construct a new lock in the child"
            raise RuntimeError(msg)
        cls._instances[normalized] = instance
        return instance

    def _reset_singletons(cls) -> None:
        cls._instances = WeakValueDictionary()
        cls._instances_lock = threading.RLock()
        cls._instances_under_construction = set()


__all__ = [
    "SoftRWMeta",
]
