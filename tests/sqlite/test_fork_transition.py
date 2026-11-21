from __future__ import annotations

import os
import sys
import textwrap
from typing import TYPE_CHECKING, Literal

import pytest

from tests.capability_marks import (
    NEEDS_FORK,
    NEEDS_FORK1,
)
from tests.read_write_helpers import OPPOSITE_MODE
from tests.sqlite.process_support import run_fork_script

if TYPE_CHECKING:
    from pathlib import Path


@NEEDS_FORK  # pragma: needs fork
@pytest.mark.parametrize(
    ("mode", "fork_name", "audit_hook"),
    [
        pytest.param("read", "fork", "allow", id="read-fork"),
        pytest.param("write", "fork", "allow", id="write-fork"),
        pytest.param("write", "fork", "reject", id="write-fork-rejected-audit-hook"),
        pytest.param(
            "read",
            "fork1",
            "allow",
            marks=NEEDS_FORK1,
            id="read-fork1",
        ),
        pytest.param(
            "write",
            "fork1",
            "allow",
            marks=NEEDS_FORK1,
            id="write-fork1",
        ),
    ],
)
def test_read_write_lock_survives_normal_fork_child_exit(
    tmp_path: Path,
    mode: Literal["read", "write"],
    fork_name: Literal["fork", "fork1"],
    audit_hook: Literal["reject", "allow"],
) -> None:
    result = run_fork_script(
        textwrap.dedent("""
        from __future__ import annotations

        import gc
        import os
        import sqlite3
        import sys
        from typing import TYPE_CHECKING

        if TYPE_CHECKING:
            from _typeshed import Unused

        lock_path, mode, opposite_mode, fork_name, audit_hook_mode = sys.argv[1:]
        if audit_hook_mode == "reject":
            def reject_filelock_audit_hook(event: str, _args: Unused) -> None:
                if event == "sys.addaudithook":
                    raise RuntimeError("audit hook registration rejected")

            sys.addaudithook(reject_filelock_audit_hook)

        from filelock import ReadWriteLock, Timeout

        lock = ReadWriteLock(lock_path, is_singleton=False)
        acquire = getattr(lock, f"acquire_{mode}")
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

                def count_sqlite_connects(event: str, _args: Unused) -> None:
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
        conflicting_acquire = getattr(contender, f"acquire_{opposite_mode}")
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
        """),
        [
            str(tmp_path / "fork.db"),
            mode,
            OPPOSITE_MODE[mode],
            fork_name,
            audit_hook,
        ],
        timeout=15,
    )

    assert result == (0, "", "")


@NEEDS_FORK  # pragma: needs fork
def test_read_write_lock_fork_waits_for_sqlite_operation(tmp_path: Path) -> None:
    result = run_fork_script(
        textwrap.dedent(r"""
        from __future__ import annotations

        import os
        import queue
        import sqlite3
        import sys
        import threading
        import time
        import warnings
        from typing import TYPE_CHECKING

        if TYPE_CHECKING:
            from _typeshed import StrOrBytesPath

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

        # Typed for the sqlite3.connect payload the hook reads; every other event returns before touching args.
        def audit_hook(event: str, args: tuple[StrOrBytesPath, ...]) -> None:
            if not armed or event != "sqlite3.connect":
                return
            database = args[0]
            if isinstance(database, (str, bytes)) and (
                (path := os.fsdecode(database)) == lock_path
                or path.startswith(("/dev/fd/", "/proc/self/fd/"))
                or os.path.basename(os.path.dirname(path)).startswith(".filelock-")
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
        """),
        [str(tmp_path / "fork-gate.db")],
        timeout=15,
    )

    assert result == (0, "", "")


@NEEDS_FORK  # pragma: needs fork
def test_read_write_lock_fork_beside_a_waiting_acquire_returns(tmp_path: Path) -> None:
    result = run_fork_script(
        textwrap.dedent(r"""
        from __future__ import annotations

        import os
        import sys
        import threading
        import warnings

        from filelock import ReadWriteLock

        warnings.filterwarnings("ignore", category=DeprecationWarning, message=r".*fork\(\).*")

        lock_path = sys.argv[1]
        holder = ReadWriteLock(lock_path, is_singleton=False)
        waiter = ReadWriteLock(lock_path, is_singleton=False)
        acquired = threading.Event()

        def acquire() -> None:
            with waiter.write_lock():
                acquired.set()

        holder.acquire_write()
        waiter_thread = threading.Thread(target=acquire, daemon=True)
        waiter_thread.start()
        assert not acquired.wait(0.2)
        # Only this thread can end the waiter's wait, so a fork that waited for it would not return.
        child_pid = os.fork()
        if child_pid == 0:
            os._exit(0)
        _, status = os.waitpid(child_pid, 0)
        holder.release()
        assert acquired.wait(5)
        waiter_thread.join(5)
        assert os.waitstatus_to_exitcode(status) == 0
        assert not waiter_thread.is_alive()
        """),
        [str(tmp_path / "fork-waiter.db")],
        timeout=15,
    )

    assert result == (0, "", "")


@pytest.mark.skipif(
    sys.implementation.name != "cpython" or not hasattr(os, "fork"),
    reason="requires CPython profile events and os.fork",
)
@pytest.mark.parametrize(
    "boundary",
    [
        pytest.param("execute", id="execute"),
        pytest.param("rollback", id="rollback"),
        pytest.param("close", id="close"),
        pytest.param("connection-return", id="connection-return"),
    ],
)
def test_read_write_lock_fork_at_sqlite_boundary_exits_child(  # pragma: needs fork
    tmp_path: Path, boundary: Literal["execute", "rollback", "close", "connection-return"]
) -> None:
    result = run_fork_script(
        textwrap.dedent("""
        from __future__ import annotations

        import os
        import sqlite3
        import sys
        from types import FrameType
        from typing import TYPE_CHECKING

        if TYPE_CHECKING:
            from _typeshed import Unused


        def reject_filelock_audit_hook(event: str, _args: Unused) -> None:
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
            value: object,
        ) -> None:
            global armed
            connection_returned = boundary == "connection-return" and event == "return" and isinstance(
                value, sqlite3.Connection
            )
            sqlite_called = (
                boundary != "connection-return"
                and event == "c_call"
                and getattr(value, "__name__", None) == boundary
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
        """),
        [str(tmp_path / "fork-boundary.db"), boundary],
        timeout=10,
    )

    assert result == (0, "", "")
