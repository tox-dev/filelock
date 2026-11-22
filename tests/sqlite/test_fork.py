from __future__ import annotations

import os
import sys
import textwrap
from typing import TYPE_CHECKING, Literal, NoReturn

import pytest
from capabilities import CAPABILITIES

from filelock import ReadWriteLock
from tests.capability_marks import (
    NEEDS_FORK,
)
from tests.fork_helpers import exit_child, fork_process
from tests.sqlite.process_support import NEEDS_FD_DIRECTORY, run_fork_script

if TYPE_CHECKING:
    from pathlib import Path

    from pytest_mock import MockerFixture


@NEEDS_FORK  # pragma: needs fork
@NEEDS_FD_DIRECTORY  # pragma: needs fd-directory
@pytest.mark.skipif(not CAPABILITIES["posix-hard-link"], reason="cleanup needs private links")
def test_private_link_sweep_releases_the_inherited_cursor(tmp_path: Path) -> None:  # pragma: needs posix-hard-link
    result = run_fork_script(
        textwrap.dedent("""
        import os
        import sys
        from pathlib import Path

        from filelock import ReadWriteLock
        from filelock._sqlite import _links
        from filelock._core import ensure_current_process
        from tests.descriptor_helpers import descriptors_on

        parent = Path(sys.argv[1])
        parent.chmod(0o777)
        node = parent.stat()
        _links._SWEEP_BUDGET = 0
        ReadWriteLock(parent / "lock.db", is_singleton=False).close()
        assert descriptors_on(node) == 1
        child = os.fork()
        if child == 0:
            ensure_current_process()
            assert descriptors_on(node) == 0
            sys.exit(0)
        _, status = os.waitpid(child, 0)
        assert os.waitstatus_to_exitcode(status) == 0
        assert descriptors_on(node) == 1
        """),
        [str(tmp_path)],
        timeout=10,
    )
    assert result == (0, "", "")


@NEEDS_FORK  # pragma: needs fork
@pytest.mark.filterwarnings("ignore:.*multi-threaded, use of fork.*:DeprecationWarning")
def test_fork_child_leaves_inherited_sqlite_connection_open(tmp_path: Path, mocker: MockerFixture) -> None:
    import sqlite3

    connect = mocker.spy(sqlite3, "connect")
    with ReadWriteLock(tmp_path / "held.db", is_singleton=False).read_lock():
        connection = connect.spy_return
        mocker.patch("filelock._sqlite._connection.GETPID", return_value=os.getpid(), autospec=True)

        def close_in_child() -> NoReturn:  # pragma: forked child
            # SQLite forbids using an inherited handle, so record whether close reaches the C extension.
            backend = mocker.patch.object(sqlite3, "Connection", autospec=True)
            connection.close()
            exit_child(int(backend.close.called))

        _, status = os.waitpid(fork_process(close_in_child), 0)
        assert os.waitstatus_to_exitcode(status) == 0


@NEEDS_FORK  # pragma: needs fork
def test_read_write_lock_idle_fork_handles_fresh_child_lock(tmp_path: Path) -> None:
    result = run_fork_script(
        textwrap.dedent("""
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
        """),
        [str(tmp_path / "idle-fork.db")],
        timeout=10,
    )

    assert result == (0, "", "")


@pytest.mark.skipif(sys.implementation.name != "pypy" or not hasattr(os, "fork"), reason="requires fork on PyPy")
def test_read_write_lock_pypy_child_allows_without_known_sqlite_use(tmp_path: Path) -> None:  # pragma: pypy cover
    result = run_fork_script(
        textwrap.dedent("""
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
        """),
        [str(tmp_path / "child.db")],
        timeout=10,
    )

    assert result == (0, "", "")


@pytest.mark.skipif(sys.implementation.name != "pypy" or not hasattr(os, "fork"), reason="requires fork on PyPy")
def test_read_write_lock_pypy_child_rejects_after_external_sqlite_use(tmp_path: Path) -> None:  # pragma: pypy cover
    result = run_fork_script(
        textwrap.dedent("""
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
        """),
        [str(tmp_path / "external.db"), str(tmp_path / "child.db")],
        timeout=10,
    )

    assert result == (0, "", "")


def test_fork_script_terminates_timed_out_process_group() -> None:
    with pytest.raises(AssertionError, match="fork script exceeded"):
        run_fork_script("import time; time.sleep(10)", [], timeout=0.01)


@NEEDS_FORK  # pragma: needs fork
def test_read_write_lock_subclass_cache_resets_after_fork(tmp_path: Path) -> None:
    result = run_fork_script(
        textwrap.dedent("""
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
        """),
        [str(tmp_path / "subclass-fork.db")],
        timeout=10,
    )

    assert result == (0, "", "")


@NEEDS_FORK  # pragma: needs fork
@pytest.mark.parametrize("state", [pytest.param("idle", id="idle"), pytest.param("held", id="held")])
def test_async_read_write_lock_fork_behavior(tmp_path: Path, state: Literal["idle", "held"]) -> None:
    result = run_fork_script(
        textwrap.dedent(r"""
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
        # Each asyncio.run is a new task and a hold belongs to the task that took it, so close() releases the parent's.
        asyncio.run(lock.close())
        with sqlite3.connect(lock_path) as connection:
            assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        """),
        [str(tmp_path / "async-fork.db"), state],
        timeout=15,
    )

    assert result == (0, "", "")
