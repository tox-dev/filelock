from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    assert __spec__ is not None

__lazy_modules__ = ["pathlib", f"{__spec__.parent}._fork"]

import pathlib
from typing import TYPE_CHECKING

from filelock.internal.core import (
    canonical,
    check_blocking,
    ensure_current_process,
    override,
    resolve_lazy_imports_before_shutdown,
    seconds,
    timeout_behavior,
)

from ._fork import GETPID, IN_POSIX_SUBINTERPRETER

if TYPE_CHECKING:
    import os
    from threading import RLock
    from weakref import WeakValueDictionary

    from ._lock import ReadWriteLock


class ReadWriteLockMeta(type):
    # Constructors and get_lock() must consult the same cache.
    _instances: WeakValueDictionary[pathlib.Path, ReadWriteLock]
    _instances_lock: RLock
    if TYPE_CHECKING:

        def _reset_class_after_fork(cls) -> None: ...

    _instances_pid: int
    _instances_under_construction: set[pathlib.Path]

    @override
    def __call__(
        cls,
        lock_file: str | os.PathLike[str],
        timeout: float = -1,
        *,
        blocking: bool = True,
        is_singleton: bool = True,
    ) -> ReadWriteLock:
        cls._prepare_interpreter()
        check_blocking(blocking)
        timeout = seconds("timeout", timeout)
        if cls._instances_pid != GETPID():
            cls._reset_class_after_fork()
        construction_pid = GETPID()
        if not is_singleton:
            instance = super().__call__(lock_file, timeout, blocking=blocking, is_singleton=is_singleton)
            _check_construction_pid(construction_pid)
            return instance

        normalized = pathlib.Path(canonical(lock_file))
        with cls._instances_lock:
            # Another thread can drop the last reference between two cache lookups.
            if (instance := cls._instances.get(normalized)) is None:
                if normalized in cls._instances_under_construction:  # pragma: no cover - exercised in an audit callback
                    msg = f"Singleton lock construction is already active for {lock_file!s}"
                    raise RuntimeError(msg)
                construction_registry = cls._instances_under_construction
                construction_registry.add(normalized)
                try:
                    instance = super().__call__(lock_file, timeout, blocking=blocking, is_singleton=is_singleton)
                finally:
                    if GETPID() == construction_pid:
                        construction_registry.discard(normalized)
                _check_construction_pid(construction_pid)
                if instance._canonical_path != normalized:  # ruff:ignore[private-member-access]  # metaclass owns the cache
                    msg = f"ReadWriteLock path changed during singleton construction: {normalized}"
                    raise RuntimeError(msg)
                cls._instances[normalized] = instance

            if (
                timeout_behavior(instance.timeout, any_negative=False) != timeout_behavior(timeout, any_negative=False)
                or instance.blocking != blocking
            ):
                msg = (
                    f"Singleton lock created with timeout={instance.timeout}, blocking={instance.blocking},"
                    f" cannot be changed to timeout={timeout}, blocking={blocking}"
                )
                raise ValueError(msg)
            return instance

    @staticmethod
    def _prepare_interpreter() -> None:
        if IN_POSIX_SUBINTERPRETER:
            msg = (
                "ReadWriteLock requires the main interpreter on POSIX; use another process or "
                "SoftReadWriteLock if the interpreter allows daemon threads"
            )
            raise RuntimeError(msg)
        ensure_current_process()
        resolve_lazy_imports_before_shutdown()


def _check_construction_pid(construction_pid: int) -> None:
    if GETPID() != construction_pid:
        msg = "ReadWriteLock construction cannot continue after fork"
        raise RuntimeError(msg)


__all__ = [
    "ReadWriteLockMeta",
]
