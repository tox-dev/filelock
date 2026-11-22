from __future__ import annotations

import subprocess  # ruff:ignore[suspicious-subprocess-import]  # isolated interpreters control at-fork registration order
import sys
from typing import TYPE_CHECKING, Literal

import pytest
from capabilities import CAPABILITIES

from filelock import BaseFileLock, has_fcntl
from filelock._core import override
from tests.capability_marks import (
    NEEDS_AUDIT_EVENTS,
    NEEDS_FORK,
    NEEDS_FORK1,
)

if TYPE_CHECKING:
    from pathlib import Path

from tests.core.fork_probes import FORK_WARNING, NEEDS_SETTRACE_SAFE_AUDIT_HOOKS


@NEEDS_FORK  # pragma: needs fork
@NEEDS_AUDIT_EVENTS
def test_import_adds_audit_hook_only_where_settrace_survives_it() -> None:
    script = """
import sys

added = []
sys.addaudithook(lambda event, _args: added.append(event) if event == "sys.addaudithook" else None)
import filelock
print(len(added))
"""
    result = subprocess.run([sys.executable, "-c", script], check=False, capture_output=True, text=True, timeout=10)

    assert (int(result.stdout) > 0) is CAPABILITIES["settrace-safe-audit-hooks"]


@NEEDS_FORK  # pragma: needs fork
@NEEDS_SETTRACE_SAFE_AUDIT_HOOKS  # pragma: needs settrace-safe-audit-hooks
@pytest.mark.parametrize(
    "event",
    [
        pytest.param("os.fork", id="fork"),
        pytest.param("os.forkpty", id="forkpty"),
        pytest.param("_posixsubprocess.fork_exec", id="fork-exec"),
    ],
)
def test_audit_rejects_process_creation_inside_descriptor_transition(
    tmp_path: Path,
    event: Literal["os.fork", "os.forkpty", "_posixsubprocess.fork_exec"],
) -> None:
    class AuditedLock(BaseFileLock):
        @override
        def _acquire(self) -> None:
            self._context.lock_file_fd = None
            if event == "_posixsubprocess.fork_exec":
                sys.audit(event, (), (), None)
            else:
                sys.audit(event)

        _release = _acquire

    with pytest.raises(RuntimeError, match="unsafe while filelock is changing descriptor ownership"):
        AuditedLock(str(tmp_path / "audit.lock"), is_singleton=False).acquire(timeout=0)


@NEEDS_FORK  # pragma: needs fork
@FORK_WARNING
@pytest.mark.parametrize(
    "fork_mode",
    [
        pytest.param("rejected-audit", id="rejected-audit-hook"),
        pytest.param(
            "fork1",
            marks=NEEDS_FORK1,
            id="fork1",
        ),
    ],
)
def test_fork_without_audit_guard_snapshots_current_transition(
    tmp_path: Path, fork_mode: Literal["rejected-audit", "fork1"]
) -> None:
    script = """
from __future__ import annotations

import os
import sys
from errno import EBADF

if sys.argv[1] == "rejected-audit":
    from typing import TYPE_CHECKING

    if TYPE_CHECKING:
        from _typeshed import Unused

    def reject_filelock_hook(event: str, _args: Unused) -> None:
        if event == "sys.addaudithook":
            raise RuntimeError

    sys.addaudithook(reject_filelock_hook)

from filelock import BaseFileLock

fork = os.fork if sys.argv[1] == "rejected-audit" else os.fork1

class ForkingLock(BaseFileLock):
    child_status = -1

    def _acquire(self) -> None:
        descriptor = self._context.lock_file_fd = os.open(self.lock_file, os.O_CREAT | os.O_RDWR, 0o600)
        child_pid = fork()
        if child_pid == 0:
            try:
                os.fstat(descriptor)
            except OSError as exception:
                os._exit(0 if exception.errno == EBADF else 1)
            os._exit(1)
        _, status = os.waitpid(child_pid, 0)
        self.child_status = os.waitstatus_to_exitcode(status)

    def _release(self) -> None:
        os.close(self._context.lock_file_fd if self._context.lock_file_fd is not None else -1)
        self._context.lock_file_fd = None

lock = ForkingLock(sys.argv[2], is_singleton=False)
lock.acquire()
lock.release()
raise SystemExit(lock.child_status)
"""
    result = subprocess.run(
        [sys.executable, "-c", script, fork_mode, str(tmp_path / "transition.lock")],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert (result.returncode, result.stderr) == (0, "")


@NEEDS_FORK  # pragma: needs fork
@FORK_WARNING
@pytest.mark.skipif(not has_fcntl, reason="fcntl.flock required")
@pytest.mark.skipif(sys.implementation.name != "cpython", reason="CPython fcntl audit event required")
@pytest.mark.parametrize(
    "fork_mode",
    [
        pytest.param("rejected-audit", id="rejected-audit-hook"),
        pytest.param(
            "fork1",
            marks=NEEDS_FORK1,
            id="fork1",
        ),
    ],
)
def test_native_descriptor_is_visible_during_flock_audit(
    tmp_path: Path, fork_mode: Literal["rejected-audit", "fork1"]
) -> None:
    script = """
from __future__ import annotations

import os
import sys
from errno import EBADF

child_status = -1
forked = False

# Typed for the fcntl.flock payload (fd, cmd); every other event returns before touching args.
def audit_hook(event: str, args: tuple[int, int]) -> None:
    global child_status, forked
    if event == "sys.addaudithook" and sys.argv[1] == "rejected-audit":
        raise RuntimeError
    if event != "fcntl.flock" or forked:
        return
    forked = True
    descriptor = args[0]
    child_pid = (os.fork if sys.argv[1] == "rejected-audit" else os.fork1)()
    if child_pid == 0:
        try:
            os.fstat(descriptor)
        except OSError as exception:
            os._exit(0 if exception.errno == EBADF else 1)
        os._exit(1)
    _, status = os.waitpid(child_pid, 0)
    child_status = os.waitstatus_to_exitcode(status)

sys.addaudithook(audit_hook)

from filelock import FileLock

lock = FileLock(sys.argv[2], is_singleton=False)
lock.acquire()
lock.release()
raise SystemExit(child_status)
"""
    result = subprocess.run(
        [sys.executable, "-c", script, fork_mode, str(tmp_path / "native.lock")],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert (result.returncode, result.stderr) == (0, "")
