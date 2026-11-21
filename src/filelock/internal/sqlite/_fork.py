from __future__ import annotations

__lazy_modules__ = ["_thread", "importlib"]

import _thread  # ruff:ignore[import-private-name]  # CPython exposes interpreter identity through _thread.
import os
import sys
import threading
from contextlib import contextmanager
from importlib import import_module
from typing import TYPE_CHECKING, Final

from filelock.internal.core import ForkResettable, override

if TYPE_CHECKING:
    import pathlib
    import sqlite3
    from collections.abc import Callable, Generator

    from _typeshed import Unused  # simplint: ignore[private-import-path]  # audit callback signature

    from ._links import DatabaseIdentity


def _in_subinterpreter() -> bool:
    if (is_main := getattr(_thread, "_is_main_interpreter", None)) is not None:
        return not is_main()
    try:
        interpreters = import_module("_xxsubinterpreters")
    except ModuleNotFoundError:
        # Embedded CPython can create interpreters without building _xxsubinterpreters.
        if sys.implementation.name != "cpython":
            return False
        try:
            ctypes = import_module("ctypes")
            state = ctypes.PYFUNCTYPE(ctypes.c_void_p)
            current = state(("PyInterpreterState_Get", ctypes.pythonapi))
            main = state(("PyInterpreterState_Main", ctypes.pythonapi))
        except (ImportError, AttributeError):
            return False
        return current() != main()
    return interpreters.get_current() != interpreters.get_main()


IN_POSIX_SUBINTERPRETER: Final[bool] = os.name == "posix" and _in_subinterpreter()
GETPID: Final[Callable[[], int]] = os.getpid
_IS_PYPY: Final[bool] = sys.implementation.name == "pypy"


class _SQLiteTransitionContext(threading.local):
    depth: int = 0


_SQLITE_TRANSITION_CONTEXT: Final[_SQLiteTransitionContext] = _SQLiteTransitionContext()


class _ConnectionEscrow(ForkResettable):
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._functions: tuple[Callable[[sqlite3.Connection], None], Callable[[sqlite3.Connection], None]] | None = None

    def functions(
        self,
    ) -> tuple[Callable[[sqlite3.Connection], None], Callable[[sqlite3.Connection], None]] | None:
        if sys.version_info >= (3, 12) or sys.implementation.name != "cpython" or not hasattr(os, "register_at_fork"):
            return None  # pragma: >=3.12 cover
        with self._lock:  # pragma: <3.12 cover  # pragma: needs fork
            if self._functions is None:
                import ctypes  # ruff:ignore[import-outside-top-level]  # only older CPython needs these symbols

                function_type = ctypes.PYFUNCTYPE(None, ctypes.py_object)
                increment_address = ctypes.cast(ctypes.pythonapi.Py_IncRef, ctypes.c_void_p).value
                decrement_address = ctypes.cast(ctypes.pythonapi.Py_DecRef, ctypes.c_void_p).value
                if increment_address is None or decrement_address is None:  # pragma: no cover - resolved CPython API
                    msg = "CPython reference functions have no address"
                    raise RuntimeError(msg)
                self._functions = (
                    function_type(increment_address),
                    function_type(decrement_address),
                )
            return self._functions

    @override
    def _reset_after_fork_in_child(self) -> None:  # pragma: forked child
        self._lock = threading.RLock()


CONNECTION_ESCROW: Final[_ConnectionEscrow] = _ConnectionEscrow()


class _ForkedDatabaseRegistry(ForkResettable):
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._paths: set[pathlib.Path] = set()
        self._identities: set[DatabaseIdentity] = set()
        self._sqlite_used = False
        self._all_paths_poisoned = False

    def raise_if_poisoned(self, path: pathlib.Path) -> None:
        identity = self.identity(path)
        with self._lock:
            all_paths_poisoned = self._all_paths_poisoned
            poisoned = (
                all_paths_poisoned or path in self._paths or (identity is not None and identity in self._identities)
            )
        if poisoned:  # pragma: needs fork
            msg = (
                "ReadWriteLock is unavailable in a PyPy fork child; exec or exit before using it"
                if all_paths_poisoned
                else f"SQLite database {path!s} was active across fork(); exec or exit before using it in the child"
            )
            raise RuntimeError(msg)

    @staticmethod
    def identity(path: pathlib.Path) -> DatabaseIdentity | None:
        try:
            stat_result = path.stat()
        except OSError:
            return None
        return stat_result.st_dev, stat_result.st_ino

    def poison_after_fork(self, path: pathlib.Path, identity: DatabaseIdentity | None) -> None:
        self._paths.add(path)
        if identity is not None:
            self._identities.add(identity)

    def note_sqlite_use(self) -> None:
        if _IS_PYPY:
            with self._lock:
                self._sqlite_used = True

    @override
    def _reset_after_fork_in_child(self) -> None:  # pragma: forked child
        self._lock = threading.RLock()
        self._all_paths_poisoned = self._all_paths_poisoned or (_IS_PYPY and self._sqlite_used)
        self._sqlite_used = False


FORKED_DATABASES: Final[_ForkedDatabaseRegistry] = _ForkedDatabaseRegistry()


@contextmanager
def sqlite_transition() -> Generator[None]:
    _SQLITE_TRANSITION_CONTEXT.depth += 1
    try:
        yield
    finally:
        _SQLITE_TRANSITION_CONTEXT.depth -= 1


def install_hooks() -> None:
    if _IS_PYPY:
        sys.addaudithook(_track_sqlite_use)  # pragma: pypy cover
    if hasattr(os, "register_at_fork"):  # pragma: needs fork
        os.register_at_fork(after_in_child=_abort_forked_sqlite_transition)


def _track_sqlite_use(event: str, _args: Unused) -> None:
    if event == "sqlite3.connect":
        FORKED_DATABASES.note_sqlite_use()


def _abort_forked_sqlite_transition() -> None:  # pragma: forked child
    if _SQLITE_TRANSITION_CONTEXT.depth:
        unsafe_fork_exit_status: Final[int] = 70
        os._exit(unsafe_fork_exit_status)  # inherited SQLite handles cannot be used or closed safely


__all__ = [
    "CONNECTION_ESCROW",
    "FORKED_DATABASES",
    "GETPID",
    "IN_POSIX_SUBINTERPRETER",
    "install_hooks",
    "sqlite_transition",
]
