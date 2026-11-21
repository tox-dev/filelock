from __future__ import annotations

import subprocess  # ruff:ignore[suspicious-subprocess-import]  # isolates process-global audit hooks and fork exits
import sys
import textwrap
from typing import TYPE_CHECKING, Literal

import pytest

from filelock import ReadWriteLock
from tests.capability_marks import (
    NEEDS_AUDIT_EVENTS,
    NEEDS_COLLECTED_FINALIZATION,
    NEEDS_FORK,
)
from tests.sqlite.process_support import run_fork_script

if TYPE_CHECKING:
    from pathlib import Path


@NEEDS_FORK  # pragma: needs fork
@NEEDS_COLLECTED_FINALIZATION
def test_async_read_write_lock_allows_finalizer_reentry(tmp_path: Path) -> None:
    result = run_fork_script(
        textwrap.dedent("""
        from __future__ import annotations

        import asyncio
        import gc
        import sys
        import weakref
        from threading import Condition, Event
        from types import CodeType, FrameType
        from typing import Final

        from filelock import AsyncReadWriteLock, ReadWriteLock

        class _FinalizerTarget:
            cycle: _FinalizerTarget

        _FINALIZED: Final[Event] = Event()

        def _close_lock() -> None:
            ReadWriteLock(sys.argv[2], is_singleton=False).close()
            _FINALIZED.set()

        _TARGET = _FinalizerTarget()
        _TARGET.cycle = _TARGET
        weakref.finalize(_TARGET, _close_lock)
        del _TARGET

        _NOTIFY_CODE: Final[CodeType] = Condition.notify_all.__code__

        def _collect_at_notify(frame: FrameType, _event: str, _argument: None) -> None:
            if frame.f_code is _NOTIFY_CODE:
                sys.settrace(None)
                gc.collect()

        sys.settrace(_collect_at_notify)
        _LOCK: Final[AsyncReadWriteLock] = AsyncReadWriteLock(sys.argv[1], is_singleton=False)
        sys.settrace(None)

        assert _FINALIZED.is_set()
        asyncio.run(_LOCK.close())
        """),
        [str(tmp_path / "async.db"), str(tmp_path / "finalizer.db")],
        timeout=10,
    )

    assert result == (0, "", "")


def test_read_write_lock_subclass_has_independent_singletons(tmp_path: Path) -> None:
    class DerivedReadWriteLock(ReadWriteLock):
        pass

    lock_path = tmp_path / "subclass.db"
    base_lock = ReadWriteLock(lock_path)
    derived_lock = DerivedReadWriteLock(lock_path)

    assert (derived_lock is DerivedReadWriteLock(lock_path), derived_lock is not base_lock) == (True, True)


@pytest.mark.parametrize(
    "audit_event",
    [
        pytest.param("sqlite3.connect", marks=NEEDS_AUDIT_EVENTS, id="sqlite-connect"),
        pytest.param(
            "ctypes.dlsym",
            marks=pytest.mark.skipif(
                sys.implementation.name != "cpython" or sys.version_info >= (3, 12) or sys.platform == "win32",
                reason="connection escrow uses this audit event only on POSIX CPython before 3.12",
            ),
            id="ctypes-dlsym",
        ),
    ],
)
def test_read_write_lock_rejects_recursive_singleton_construction(tmp_path: Path, audit_event: str) -> None:
    result = subprocess.run(  # executes this test's fixed interpreter and source
        [
            sys.executable,
            "-c",
            textwrap.dedent("""
        from __future__ import annotations

        import sys

        from filelock import ReadWriteLock

        lock_path, audit_event = sys.argv[1:]
        armed = True

        from typing import TYPE_CHECKING

        if TYPE_CHECKING:
            from _typeshed import Unused

        def audit_hook(event: str, _args: Unused) -> None:
            if armed and event == audit_event:
                ReadWriteLock(lock_path)

        sys.addaudithook(audit_hook)
        try:
            ReadWriteLock(lock_path)
        except RuntimeError as error:
            assert str(error) == f"Singleton lock construction is already active for {lock_path}"
        else:
            raise AssertionError("recursive singleton construction succeeded")

        armed = False
        with ReadWriteLock(lock_path).read_lock():
            pass
        """),
            str(tmp_path / "construction.db"),
            audit_event,
        ],
        capture_output=True,
        check=False,
        text=True,
        timeout=10,
    )

    assert (result.returncode, result.stderr) == (0, "")


@pytest.mark.skipif(
    sys.implementation.name != "cpython" or sys.version_info >= (3, 12),
    reason="connection escrow uses CPython reference functions before 3.12",
)
def test_read_write_lock_escrow_ignores_shared_ctypes_signatures(tmp_path: Path) -> None:  # pragma: <3.12 cover
    result = subprocess.run(  # executes this test's fixed interpreter and source
        [
            sys.executable,
            "-c",
            textwrap.dedent("""
        from __future__ import annotations

        import ctypes
        import sys

        ctypes.pythonapi.Py_IncRef.argtypes = (ctypes.c_void_p,)
        from filelock import ReadWriteLock

        lock = ReadWriteLock(sys.argv[1], is_singleton=False)
        lock.acquire_read()
        ctypes.pythonapi.Py_DecRef.argtypes = (ctypes.c_void_p,)
        lock.release()
        with ReadWriteLock(sys.argv[1], is_singleton=False).write_lock():
            pass
        """),
            str(tmp_path / "ctypes.db"),
        ],
        capture_output=True,
        check=False,
        text=True,
        timeout=10,
    )

    assert (result.returncode, result.stderr) == (0, "")


@pytest.mark.parametrize(
    "operation",
    [
        pytest.param("acquire", id="acquire"),
        pytest.param("release", id="release"),
        pytest.param("close", id="close"),
    ],
)
@NEEDS_AUDIT_EVENTS
def test_read_write_lock_rejects_same_thread_operation_during_acquisition(
    tmp_path: Path, operation: Literal["acquire", "release", "close"]
) -> None:
    result = subprocess.run(  # executes this test's fixed interpreter and source
        [
            sys.executable,
            "-c",
            textwrap.dedent("""
        from __future__ import annotations

        import sys

        from filelock import ReadWriteLock

        lock_path, operation = sys.argv[1:]
        lock = ReadWriteLock(lock_path)
        armed = True

        from typing import TYPE_CHECKING

        if TYPE_CHECKING:
            from _typeshed import Unused

        def audit_hook(event: str, _args: Unused) -> None:
            if armed and event == "sqlite3.connect":
                if operation == "acquire":
                    lock.acquire_read()
                elif operation == "release":
                    lock.release()
                else:
                    lock.close()

        sys.addaudithook(audit_hook)
        try:
            lock.acquire_read()
        except RuntimeError as error:
            assert str(error) == (
                f"Cannot {operation} ReadWriteLock on {lock_path} while acquisition is active in this thread"
            )
        else:
            raise AssertionError(f"recursive {operation} succeeded")

        armed = False
        with lock.read_lock():
            pass
        """),
            str(tmp_path / "acquisition.db"),
            operation,
        ],
        capture_output=True,
        check=False,
        text=True,
        timeout=10,
    )

    assert (result.returncode, result.stderr) == (0, "")


@pytest.mark.parametrize(
    "operation",
    [
        pytest.param("release", id="release"),
        pytest.param("close", id="close"),
    ],
)
@NEEDS_AUDIT_EVENTS
def test_read_write_lock_serializes_other_thread_operation_during_acquisition(
    tmp_path: Path, operation: Literal["release", "close"]
) -> None:
    result = subprocess.run(  # executes this test's fixed interpreter and source
        [
            sys.executable,
            "-c",
            textwrap.dedent("""
        from __future__ import annotations

        import queue
        import sqlite3
        import sys
        import threading

        from filelock import ReadWriteLock

        lock_path, operation = sys.argv[1:]
        lock = ReadWriteLock(lock_path)
        acquisition_inside_connect = threading.Event()
        continue_acquisition = threading.Event()
        acquire_errors: queue.SimpleQueue[BaseException] = queue.SimpleQueue()
        operation_errors: queue.SimpleQueue[BaseException] = queue.SimpleQueue()
        armed = True

        from typing import TYPE_CHECKING

        if TYPE_CHECKING:
            from _typeshed import Unused

        def audit_hook(event: str, _args: Unused) -> None:
            if armed and event == "sqlite3.connect":
                acquisition_inside_connect.set()
                assert continue_acquisition.wait(5)

        def acquire() -> None:
            try:
                lock.acquire_write()
            except BaseException as error:
                acquire_errors.put(error)

        def operate() -> None:
            try:
                getattr(lock, operation)()
            except BaseException as error:
                operation_errors.put(error)

        sys.addaudithook(audit_hook)
        acquire_thread = threading.Thread(target=acquire)
        acquire_thread.start()
        assert acquisition_inside_connect.wait(5)
        operation_thread = threading.Thread(target=operate)
        operation_thread.start()
        operation_thread.join(0.1)
        assert operation_thread.is_alive()
        armed = False
        continue_acquisition.set()
        acquire_thread.join(5)
        operation_thread.join(5)
        assert not acquire_thread.is_alive()
        assert not operation_thread.is_alive()
        assert acquire_errors.empty()
        if operation == "release":
            assert "while it is held by thread" in str(operation_errors.get_nowait())
            lock.release(force=True)
            with lock.write_lock():
                pass
        else:
            assert operation_errors.empty()
            try:
                lock.acquire_read()
            except sqlite3.ProgrammingError as error:
                assert str(error) == "Cannot operate on a closed database."
            else:
                raise AssertionError("concurrent acquisition reopened a closed lock")
        """),
            str(tmp_path / "concurrent.db"),
            operation,
        ],
        capture_output=True,
        check=False,
        text=True,
        timeout=10,
    )

    assert (result.returncode, result.stderr) == (0, "")
