from __future__ import annotations

import contextlib
import os
import signal
import subprocess  # ruff:ignore[suspicious-subprocess-import]  # isolates process-global audit hooks and fork exits
import sys
import textwrap
from pathlib import Path
from typing import Literal

import pytest

from filelock import ReadWriteLock


def test_read_write_lock_closes_idle_connections(tmp_path: Path) -> None:
    lock_path = tmp_path / "idle.db"
    connection_events = 0

    def audit_hook(  # pragma: no cover - interpreter audit hooks run with tracing disabled
        event: str, args: tuple[object, ...]
    ) -> None:
        nonlocal connection_events
        if event != "sqlite3.connect":
            return
        database = args[0]
        if isinstance(database, (str, bytes)) and os.fsdecode(database) == str(lock_path):
            connection_events += 1

    sys.addaudithook(audit_hook)
    lock = ReadWriteLock(lock_path, is_singleton=False)
    lock.acquire_read()
    lock.acquire_read()
    lock.release()
    lock.release()
    lock.acquire_write()
    lock.release()
    lock_path.unlink()

    assert connection_events == 3


@pytest.mark.skipif(
    not Path("/dev/fd").is_dir() and not Path("/proc/self/fd").is_dir(),
    reason="no descriptor view",
)
def test_read_write_lock_dropped_instances_leave_no_descriptors(tmp_path: Path) -> None:  # pragma: win32 no cover
    result = _run_fork_script(_dropped_instances_script(), [str(tmp_path)], timeout=10)

    assert result == (0, "", "")


@pytest.mark.skipif(not hasattr(os, "register_at_fork"), reason="requires fork transitions")
def test_async_read_write_lock_allows_finalizer_reentry(tmp_path: Path) -> None:  # pragma: win32 no cover
    result = _run_fork_script(
        _reentrant_finalizer_script(),
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
        pytest.param("sqlite3.connect", id="sqlite-connect"),
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
        [sys.executable, "-c", _recursive_construction_script(), str(tmp_path / "construction.db"), audit_event],
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
def test_read_write_lock_escrow_ignores_shared_ctypes_signatures(tmp_path: Path) -> None:
    result = subprocess.run(  # executes this test's fixed interpreter and source
        [sys.executable, "-c", _ctypes_signature_script(), str(tmp_path / "ctypes.db")],
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
def test_read_write_lock_rejects_same_thread_operation_during_acquisition(
    tmp_path: Path, operation: Literal["acquire", "release", "close"]
) -> None:
    result = subprocess.run(  # executes this test's fixed interpreter and source
        [sys.executable, "-c", _recursive_acquisition_script(), str(tmp_path / "acquisition.db"), operation],
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
def test_read_write_lock_serializes_other_thread_operation_during_acquisition(
    tmp_path: Path, operation: Literal["release", "close"]
) -> None:
    result = subprocess.run(  # executes this test's fixed interpreter and source
        [sys.executable, "-c", _concurrent_operation_script(), str(tmp_path / "concurrent.db"), operation],
        capture_output=True,
        check=False,
        text=True,
        timeout=10,
    )

    assert (result.returncode, result.stderr) == (0, "")


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires os.fork")
@pytest.mark.parametrize(
    ("mode", "fork_name", "reject_audit_hook"),
    [
        pytest.param("read", "fork", False, id="read-fork"),
        pytest.param("write", "fork", False, id="write-fork"),
        pytest.param("write", "fork", True, id="write-fork-rejected-audit-hook"),
        pytest.param(
            "read",
            "fork1",
            False,
            marks=pytest.mark.skipif(not hasattr(os, "fork1"), reason="requires os.fork1"),
            id="read-fork1",
        ),
        pytest.param(
            "write",
            "fork1",
            False,
            marks=pytest.mark.skipif(not hasattr(os, "fork1"), reason="requires os.fork1"),
            id="write-fork1",
        ),
    ],
)
def test_read_write_lock_survives_normal_fork_child_exit(  # pragma: win32 no cover
    tmp_path: Path,
    mode: Literal["read", "write"],
    fork_name: Literal["fork", "fork1"],
    reject_audit_hook: bool,
) -> None:
    result = _run_fork_script(
        _fork_script(),
        [
            str(tmp_path / "fork.db"),
            mode,
            fork_name,
            "reject" if reject_audit_hook else "allow",
        ],
        timeout=15,
    )

    assert result == (0, "", "")


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires os.fork")
def test_read_write_lock_fork_waits_for_sqlite_operation(tmp_path: Path) -> None:  # pragma: win32 no cover
    result = _run_fork_script(_fork_during_sqlite_script(), [str(tmp_path / "fork-gate.db")], timeout=15)

    assert result == (0, "", "")


@pytest.mark.skipif(
    sys.implementation.name != "cpython" or not hasattr(os, "fork"),
    reason="requires CPython profile events and os.fork",
)
@pytest.mark.parametrize(
    "boundary",
    [
        pytest.param("executescript", id="execute"),
        pytest.param("rollback", id="rollback"),
        pytest.param("close", id="close"),
        pytest.param("connection-return", id="connection-return"),
    ],
)
def test_read_write_lock_fork_at_sqlite_boundary_exits_child(  # pragma: win32 no cover
    tmp_path: Path, boundary: Literal["executescript", "rollback", "close", "connection-return"]
) -> None:
    result = _run_fork_script(
        _fork_at_sqlite_boundary_script(),
        [str(tmp_path / "fork-boundary.db"), boundary],
        timeout=10,
    )

    assert result == (0, "", "")


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires os.fork")
def test_read_write_lock_idle_fork_handles_fresh_child_lock(tmp_path: Path) -> None:  # pragma: win32 no cover
    result = _run_fork_script(_idle_fork_script(), [str(tmp_path / "idle-fork.db")], timeout=10)

    assert result == (0, "", "")


@pytest.mark.skipif(sys.implementation.name != "pypy" or not hasattr(os, "fork"), reason="requires fork on PyPy")
def test_read_write_lock_pypy_child_allows_without_known_sqlite_use(tmp_path: Path) -> None:
    result = _run_fork_script(
        _pypy_fork_script(),
        [str(tmp_path / "child.db")],
        timeout=10,
    )

    assert result == (0, "", "")


@pytest.mark.skipif(sys.implementation.name != "pypy" or not hasattr(os, "fork"), reason="requires fork on PyPy")
def test_read_write_lock_pypy_child_rejects_after_external_sqlite_use(tmp_path: Path) -> None:
    result = _run_fork_script(
        _pypy_external_sqlite_script(),
        [str(tmp_path / "external.db"), str(tmp_path / "child.db")],
        timeout=10,
    )

    assert result == (0, "", "")


def test_fork_script_terminates_timed_out_process_group() -> None:
    with pytest.raises(AssertionError, match="fork script exceeded"):
        _run_fork_script("import time; time.sleep(10)", [], timeout=0.01)


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires os.fork")
def test_read_write_lock_subclass_cache_resets_after_fork(tmp_path: Path) -> None:  # pragma: win32 no cover
    result = _run_fork_script(_subclass_fork_script(), [str(tmp_path / "subclass-fork.db")], timeout=10)

    assert result == (0, "", "")


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires os.fork")
@pytest.mark.parametrize("held", [pytest.param(False, id="idle"), pytest.param(True, id="held")])
def test_async_read_write_lock_fork_behavior(tmp_path: Path, held: bool) -> None:  # pragma: win32 no cover
    result = _run_fork_script(
        _async_fork_script(),
        [str(tmp_path / "async-fork.db"), "held" if held else "idle"],
        timeout=15,
    )

    assert result == (0, "", "")


def _run_fork_script(script: str, arguments: list[str], *, timeout: float) -> tuple[int, str, str]:
    process = subprocess.Popen(  # executes this test's fixed interpreter and source
        [sys.executable, "-c", script, *arguments],
        start_new_session=True,
        stderr=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        process_output, process_error = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as error:
        with contextlib.suppress(ProcessLookupError):
            if sys.platform == "win32":  # pragma: win32 cover
                process.kill()
            else:  # pragma: win32 no cover
                os.killpg(process.pid, signal.SIGKILL)
        process_output, process_error = process.communicate()
        msg = f"fork script exceeded {timeout} seconds: stdout={process_output!r}, stderr={process_error!r}"
        raise AssertionError(msg) from error
    assert process.returncode is not None  # pragma: win32 no cover
    return process.returncode, process_output, process_error  # pragma: win32 no cover


def _dropped_instances_script() -> str:  # pragma: win32 no cover
    return textwrap.dedent(
        """
        from __future__ import annotations

        import gc
        import sys
        from pathlib import Path

        from filelock import ReadWriteLock

        root = Path(sys.argv[1])
        descriptor_path = Path("/dev/fd") if Path("/dev/fd").is_dir() else Path("/proc/self/fd")
        gc.collect()
        initial_count = sum(1 for _ in descriptor_path.iterdir())
        for index in range(100):
            lock = ReadWriteLock(root / f"dropped-{index}.db", is_singleton=False)
            lock.acquire_write()
            del lock
        gc.collect()

        assert sum(1 for _ in descriptor_path.iterdir()) == initial_count
        with ReadWriteLock(root / "dropped-0.db", is_singleton=False).read_lock():
            pass
        """
    )


def _reentrant_finalizer_script() -> str:  # pragma: win32 no cover
    return textwrap.dedent(
        """
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
        """
    )


def _recursive_construction_script() -> str:
    return textwrap.dedent(
        """
        from __future__ import annotations

        import sys

        from filelock import ReadWriteLock

        lock_path, audit_event = sys.argv[1:]
        armed = True

        def audit_hook(event: str, _args: tuple[object, ...]) -> None:
            # CPython supplies heterogeneous values for process-wide audit events.
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
        """
    )


def _recursive_acquisition_script() -> str:
    return textwrap.dedent(
        """
        from __future__ import annotations

        import sys

        from filelock import ReadWriteLock

        lock_path, operation = sys.argv[1:]
        lock = ReadWriteLock(lock_path)
        armed = True

        def audit_hook(event: str, _args: tuple[object, ...]) -> None:
            # CPython supplies heterogeneous values for process-wide audit events.
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
        """
    )


def _ctypes_signature_script() -> str:
    return textwrap.dedent(
        """
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
        """
    )


def _concurrent_operation_script() -> str:
    return textwrap.dedent(
        """
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

        def audit_hook(event: str, _args: tuple[object, ...]) -> None:
            # CPython supplies heterogeneous values for process-wide audit events.
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
        assert operation_errors.empty()
        if operation == "release":
            with lock.write_lock():
                pass
        else:
            try:
                lock.acquire_read()
            except sqlite3.ProgrammingError as error:
                assert str(error) == "Cannot operate on a closed database."
            else:
                raise AssertionError("concurrent acquisition reopened a closed lock")
        """
    )


def _fork_script() -> str:
    return textwrap.dedent(
        """
        from __future__ import annotations

        import gc
        import os
        import sqlite3
        import sys

        lock_path, mode, fork_name, audit_hook_mode = sys.argv[1:]
        if audit_hook_mode == "reject":
            def reject_filelock_audit_hook(event: str, _args: tuple[object, ...]) -> None:
                # CPython supplies heterogeneous values for process-wide audit events.
                if event == "sys.addaudithook":
                    raise RuntimeError("audit hook registration rejected")

            sys.addaudithook(reject_filelock_audit_hook)

        from filelock import ReadWriteLock, Timeout

        lock = ReadWriteLock(lock_path, is_singleton=False)
        acquire = lock.acquire_read if mode == "read" else lock.acquire_write
        proxy = acquire()
        parent_to_child_read, parent_to_child_write = os.pipe()
        child_to_parent_read, child_to_parent_write = os.pipe()

        child_pid = getattr(os, fork_name)()
        if child_pid == 0:
            os.close(parent_to_child_write)
            os.close(child_to_parent_read)
            try:
                with proxy:
                    pass
                try:
                    lock.acquire_read()
                except RuntimeError as error:
                    assert "was invalidated by fork()" in str(error)
                else:
                    raise AssertionError("inherited acquisition succeeded")
                lock.release()
                lock.close()
                del proxy
                del lock
                gc.collect()
                sqlite_connects = 0

                def count_sqlite_connects(event: str, _args: tuple[object, ...]) -> None:
                    # CPython supplies heterogeneous values for process-wide audit events.
                    global sqlite_connects
                    if event == "sqlite3.connect":
                        sqlite_connects += 1

                sys.addaudithook(count_sqlite_connects)
                try:
                    ReadWriteLock(lock_path, is_singleton=False)
                except RuntimeError as error:
                    expected = (
                        "ReadWriteLock is unavailable in a PyPy fork child"
                        if sys.implementation.name == "pypy"
                        else "was active across fork(); exec or exit"
                    )
                    assert expected in str(error)
                else:
                    raise AssertionError("child reopened a database active during fork")
                assert sqlite_connects == 0
                os.write(child_to_parent_write, b"1")
                assert os.read(parent_to_child_read, 1) == b"1"
            except BaseException:
                import traceback

                traceback.print_exc()
                sys.exit(1)
            sys.exit(0)

        os.close(parent_to_child_read)
        os.close(child_to_parent_write)
        assert os.read(child_to_parent_read, 1) == b"1"
        contender = ReadWriteLock(lock_path, is_singleton=False)
        conflicting_acquire = contender.acquire_write if mode == "read" else contender.acquire_read
        try:
            conflicting_acquire(blocking=False)
        except Timeout:
            pass
        else:
            raise AssertionError("child exit released the parent's lock")
        lock.release()
        os.write(parent_to_child_write, b"1")
        _, status = os.waitpid(child_pid, 0)
        assert os.waitstatus_to_exitcode(status) == 0
        with sqlite3.connect(lock_path) as connection:
            assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        """
    )


def _fork_during_sqlite_script() -> str:  # pragma: win32 no cover
    return textwrap.dedent(
        r"""
        from __future__ import annotations

        import os
        import queue
        import sqlite3
        import sys
        import threading
        import time
        import warnings

        from filelock import ReadWriteLock

        warnings.filterwarnings("ignore", category=DeprecationWarning, message=r".*fork\(\).*")

        lock_path = sys.argv[1]
        waiter = ReadWriteLock(lock_path, is_singleton=False)
        holder = sqlite3.connect(lock_path, check_same_thread=False)
        holder.executescript("PRAGMA journal_mode=MEMORY; BEGIN EXCLUSIVE TRANSACTION;").close()
        waiter_inside_connect = threading.Event()
        continue_connect = threading.Event()
        acquisition_errors: queue.SimpleQueue[BaseException] = queue.SimpleQueue()
        armed = True

        def audit_hook(event: str, args: tuple[object, ...]) -> None:
            # CPython supplies heterogeneous values for process-wide audit events.
            if not armed or event != "sqlite3.connect":
                return
            database = args[0]
            if (
                isinstance(database, (str, bytes))
                and os.fsdecode(database) == lock_path
            ):
                waiter_inside_connect.set()
                assert continue_connect.wait(5)

        def acquire() -> None:
            try:
                with waiter.read_lock(timeout=5):
                    pass
            except BaseException as error:
                acquisition_errors.put(error)

        def release_holder() -> None:
            threading.Event().wait(0.2)
            continue_connect.set()
            holder.rollback()
            holder.close()

        sys.addaudithook(audit_hook)
        waiter_thread = threading.Thread(target=acquire)
        waiter_thread.start()
        assert waiter_inside_connect.wait(5)
        release_thread = threading.Thread(target=release_holder)
        release_thread.start()
        start = time.monotonic()
        child_pid = os.fork()
        elapsed = time.monotonic() - start
        if child_pid == 0:
            sys.exit(0)
        armed = False
        _, status = os.waitpid(child_pid, 0)
        waiter_thread.join(5)
        release_thread.join(5)
        assert os.waitstatus_to_exitcode(status) == 0
        assert elapsed >= 0.1
        assert not waiter_thread.is_alive()
        assert not release_thread.is_alive()
        if not acquisition_errors.empty():
            raise acquisition_errors.get()
        """
    )


def _fork_at_sqlite_boundary_script() -> str:
    return textwrap.dedent(
        """
        from __future__ import annotations

        import os
        import sqlite3
        import sys
        from types import FrameType
        from typing import Protocol

        class ProfiledCall(Protocol):
            @property
            def __name__(self) -> str: ...

        def reject_filelock_audit_hook(event: str, _args: tuple[object, ...]) -> None:
            # CPython audit events have heterogeneous interpreter-owned payloads.
            if event == "sys.addaudithook":
                raise RuntimeError("audit hook registration rejected")

        sys.addaudithook(reject_filelock_audit_hook)

        from filelock import ReadWriteLock

        lock_path, boundary = sys.argv[1:]
        child_pids: list[int] = []
        armed = True

        def profile(
            _frame: FrameType,
            event: str,
            value: ProfiledCall | sqlite3.Connection | None,
        ) -> None:
            global armed
            connection_returned = boundary == "connection-return" and event == "return" and isinstance(
                value, sqlite3.Connection
            )
            sqlite_called = (
                boundary != "connection-return"
                and event == "c_call"
                and value is not None
                and value.__name__ == boundary
            )
            if armed and (connection_returned or sqlite_called):
                armed = False
                child_pids.append(os.fork())

        sys.setprofile(profile)
        with ReadWriteLock(lock_path, is_singleton=False).write_lock():
            pass
        sys.setprofile(None)

        assert len(child_pids) == 1
        _, status = os.waitpid(child_pids[0], 0)
        assert os.waitstatus_to_exitcode(status) == 70
        """
    )


def _idle_fork_script() -> str:
    return textwrap.dedent(
        """
        from __future__ import annotations

        import os
        import sys

        from filelock import ReadWriteLock

        lock_path = sys.argv[1]
        inherited_lock = ReadWriteLock(lock_path, is_singleton=False)
        child_pid = os.fork()
        if child_pid == 0:
            try:
                inherited_lock.acquire_read()
            except RuntimeError as error:
                assert "was invalidated by fork()" in str(error)
            else:
                raise AssertionError("inherited idle lock acquired")
            if sys.implementation.name == "pypy":
                try:
                    ReadWriteLock(lock_path, is_singleton=False)
                except RuntimeError as error:
                    assert str(error) == (
                        "ReadWriteLock is unavailable in a PyPy fork child; exec or exit before using it"
                    )
                else:
                    raise AssertionError("PyPy child opened SQLite after fork")
            else:
                with ReadWriteLock(lock_path, is_singleton=False).write_lock():
                    pass
            sys.exit(0)
        _, status = os.waitpid(child_pid, 0)
        assert os.waitstatus_to_exitcode(status) == 0
        """
    )


def _pypy_fork_script() -> str:
    return textwrap.dedent(
        """
        from __future__ import annotations

        import os
        import sys

        from filelock import ReadWriteLock

        child_path = sys.argv[1]
        child_pid = os.fork()
        if child_pid == 0:
            with ReadWriteLock(child_path, is_singleton=False).read_lock():
                pass
            sys.exit(0)
        _, status = os.waitpid(child_pid, 0)
        assert os.waitstatus_to_exitcode(status) == 0
        """
    )


def _pypy_external_sqlite_script() -> str:
    return textwrap.dedent(
        """
        from __future__ import annotations

        import os
        import sqlite3
        import sys

        from filelock import ReadWriteLock

        external_path, child_path = sys.argv[1:]
        sqlite3.connect(external_path).close()
        child_pid = os.fork()
        if child_pid == 0:
            try:
                ReadWriteLock(child_path, is_singleton=False)
            except RuntimeError as error:
                assert str(error) == (
                    "ReadWriteLock is unavailable in a PyPy fork child; exec or exit before using it"
                )
            else:
                raise AssertionError("PyPy child opened SQLite after parent use")
            sys.exit(0)
        _, status = os.waitpid(child_pid, 0)
        assert os.waitstatus_to_exitcode(status) == 0
        """
    )


def _subclass_fork_script() -> str:
    return textwrap.dedent(
        """
        from __future__ import annotations

        import os
        import sys

        from filelock import ReadWriteLock

        class DerivedReadWriteLock(ReadWriteLock):
            pass

        lock_path = sys.argv[1]
        inherited_lock = DerivedReadWriteLock(lock_path)
        child_pid = os.fork()
        if child_pid == 0:
            if sys.implementation.name == "pypy":
                try:
                    DerivedReadWriteLock(lock_path)
                except RuntimeError as error:
                    assert str(error) == (
                        "ReadWriteLock is unavailable in a PyPy fork child; exec or exit before using it"
                    )
                else:
                    raise AssertionError("PyPy child constructed a lock after fork")
            else:
                child_lock = DerivedReadWriteLock(lock_path)
                assert child_lock is not inherited_lock
                with child_lock.read_lock():
                    pass
            sys.exit(0)
        _, status = os.waitpid(child_pid, 0)
        assert os.waitstatus_to_exitcode(status) == 0
        """
    )


def _async_fork_script() -> str:
    return textwrap.dedent(
        r"""
        from __future__ import annotations

        import asyncio
        import gc
        import os
        import sqlite3
        import sys
        import warnings

        from filelock import AsyncAcquireReadWriteReturnProxy, AsyncReadWriteLock, ReadWriteLock, Timeout

        warnings.filterwarnings("ignore", category=DeprecationWarning, message=r".*fork\(\).*")
        lock_path, state = sys.argv[1:]

        async def set_up_parent() -> tuple[AsyncReadWriteLock, AsyncAcquireReadWriteReturnProxy | None]:
            parent_lock = AsyncReadWriteLock(lock_path, is_singleton=False)
            parent_proxy = await parent_lock.acquire_write() if state == "held" else None
            return parent_lock, parent_proxy

        lock, proxy = asyncio.run(set_up_parent())
        child_pid = os.fork()
        if child_pid == 0:
            async def check_child() -> None:
                if proxy is not None:
                    async with proxy:
                        pass
                try:
                    await lock.acquire_read()
                except RuntimeError as error:
                    assert "was invalidated by fork()" in str(error)
                else:
                    raise AssertionError("inherited async lock acquired")
                await lock.release()
                await lock.close()
                if sys.implementation.name == "pypy":
                    try:
                        AsyncReadWriteLock(lock_path, is_singleton=False)
                    except RuntimeError as error:
                        assert str(error) == (
                            "ReadWriteLock is unavailable in a PyPy fork child; exec or exit before using it"
                        )
                    else:
                        raise AssertionError("PyPy child constructed an async lock after fork")
                elif state == "idle":
                    fresh_lock = AsyncReadWriteLock(lock_path, is_singleton=False)
                    async with fresh_lock.write_lock():
                        pass
                    await fresh_lock.close()
                else:
                    try:
                        ReadWriteLock(lock_path, is_singleton=False)
                    except RuntimeError as error:
                        expected = (
                            "ReadWriteLock is unavailable in a PyPy fork child"
                            if sys.implementation.name == "pypy"
                            else "was active across fork(); exec or exit"
                        )
                        assert expected in str(error)
                    else:
                        raise AssertionError("active async database reopened in child")

            asyncio.run(check_child())
            del lock
            gc.collect()
            sys.exit(0)

        _, status = os.waitpid(child_pid, 0)
        assert os.waitstatus_to_exitcode(status) == 0
        if state == "held":
            contender = ReadWriteLock(lock_path, is_singleton=False)
            try:
                contender.acquire_read(blocking=False)
            except Timeout:
                pass
            else:
                raise AssertionError("async child cleanup released the parent's lock")
            asyncio.run(lock.release())
        asyncio.run(lock.close())
        with sqlite3.connect(lock_path) as connection:
            assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        """
    )
