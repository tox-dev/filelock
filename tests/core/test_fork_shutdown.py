from __future__ import annotations

import subprocess  # ruff:ignore[suspicious-subprocess-import]  # isolated interpreters control at-fork registration order
import sys
from typing import TYPE_CHECKING, Final

import pytest

from tests.capability_marks import (
    NEEDS_FORK,
    NEEDS_PROMPT_FINALIZATION,
    NEEDS_REGISTER_AT_FORK,
)
from tests.core.fork_probes import NEEDS_SETTRACE_SAFE_AUDIT_HOOKS

if TYPE_CHECKING:
    from pathlib import Path


@NEEDS_REGISTER_AT_FORK  # pragma: needs register-at-fork
@NEEDS_PROMPT_FINALIZATION
@pytest.mark.parametrize("held", [pytest.param("gate", id="gate"), pytest.param("registry_lock", id="registry-lock")])
def test_lock_finalized_at_exit_skips_fork_bookkeeping_a_frozen_daemon_thread_holds(tmp_path: Path, held: str) -> None:
    # A daemon stopped during fork bookkeeping keeps its mutex through shutdown.
    # The daemon keeps __main__ alive, so the lock must live in a separate module.
    script: Final[str] = """
import sys, threading, types
from filelock import FileLock
from filelock._core._fork import _FORK_STATE

inside = threading.Event()

def stop_inside_fork_bookkeeping():
    with getattr(_FORK_STATE, sys.argv[2]):
        inside.set()
        threading.Event().wait()

holder = sys.modules["holder"] = types.ModuleType("holder")
holder.lock = FileLock(sys.argv[1])
holder.lock.acquire()
threading.Thread(target=stop_inside_fork_bookkeeping, daemon=True).start()
inside.wait()
"""
    result: Final[subprocess.CompletedProcess[str]] = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path / "a"), held],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert (result.returncode, result.stderr) == (0, "")


@NEEDS_REGISTER_AT_FORK  # pragma: needs register-at-fork
def test_child_forked_by_a_daemon_thread_at_exit_keeps_its_fork_bookkeeping(tmp_path: Path) -> None:
    # Children forked by a daemon during atexit still need descriptor tracking.
    script: Final[str] = """
import atexit, os, sys, threading
exiting, forked = threading.Event(), threading.Event()
atexit.register(lambda: (exiting.set(), forked.wait(10)))
from filelock import FileLock
from filelock._core._fork import _OWNED_DESCRIPTORS

def fork_during_exit():
    exiting.wait()
    if (pid := os.fork()) == 0:
        for _ in range(20):
            with FileLock(sys.argv[1]):
                pass
        print(len(_OWNED_DESCRIPTORS), flush=True)
        os._exit(0)
    os.waitpid(pid, 0)
    forked.set()

threading.Thread(target=fork_during_exit, daemon=True).start()
"""
    result: Final[subprocess.CompletedProcess[str]] = subprocess.run(
        [sys.executable, "-W", "ignore::DeprecationWarning", "-c", script, str(tmp_path / "a")],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert (result.stdout, result.stderr) == ("0\n", "")


@NEEDS_FORK  # pragma: needs fork
@NEEDS_SETTRACE_SAFE_AUDIT_HOOKS  # pragma: needs settrace-safe-audit-hooks
def test_audit_hook_survives_interpreter_shutdown() -> None:
    script = """
from __future__ import annotations

from collections.abc import Callable

from filelock._core import _fork

class LateAudit:
    def __del__(self, object_id: Callable[[LateAudit], int] = id) -> None:
        object_id(self)

_fork.__dict__["_late_shutdown_audit"] = LateAudit()
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert (result.returncode, result.stderr) == (0, "")
