from __future__ import annotations

import subprocess  # ruff:ignore[suspicious-subprocess-import]  # isolated interpreters control at-fork registration order
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING, Literal

import pytest

from filelock import BaseFileLock, Timeout
from filelock._core import fork_transition, override
from filelock._core._fork import _FORK_STATE
from tests.capability_marks import (
    NEEDS_FORK,
)

if TYPE_CHECKING:
    from pathlib import Path

from tests.core.fork_probes import FORK_WARNING


@NEEDS_FORK  # pragma: needs fork
@FORK_WARNING
def test_callbacks_registered_before_filelock_can_use_locks(tmp_path: Path) -> None:
    script = """
from __future__ import annotations

import os
import sys

callback_lock = None
child_ok = False
parent_callback_ran = False

def before() -> None:
    global callback_lock
    callback_lock = FileLock(sys.argv[2], is_singleton=True)
    callback_lock.acquire()

def parent() -> None:
    global parent_callback_ran
    if callback_lock is None or not callback_lock.is_locked:
        return
    callback_lock.release()
    parent_callback_ran = True

def child() -> None:
    global child_ok
    if callback_lock is None:
        return
    child_singleton = FileLock(sys.argv[1], is_singleton=True)
    child_ok = (
        not callback_lock.is_locked
        and callback_lock.lock_counter == 0
        and child_singleton is not parent_singleton
    )

os.register_at_fork(before=before, after_in_parent=parent, after_in_child=child)

from filelock import FileLock

parent_singleton = FileLock(sys.argv[1], is_singleton=True)
child_pid = os.fork()
if child_pid == 0:
    os._exit(0 if child_ok else 1)
_, status = os.waitpid(child_pid, 0)
if (
    os.waitstatus_to_exitcode(status) != 0
    or not parent_callback_ran
    or callback_lock is None
    or callback_lock.is_locked
):
    sys.exit(2)
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path / "singleton.lock"), str(tmp_path / "callback.lock")],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert (result.returncode, result.stderr) == (0, "")


@NEEDS_FORK  # pragma: needs fork
@FORK_WARNING
@pytest.mark.parametrize(
    "audit_mode",
    [pytest.param("installed", id="audit-installed"), pytest.param("rejected", id="audit-rejected")],
)
def test_concurrent_fork_after_first_snapshot_does_not_deadlock(
    tmp_path: Path, audit_mode: Literal["installed", "rejected"]
) -> None:
    script = """
from __future__ import annotations

import os
import sys
import threading
from queue import Queue

entered = threading.Event()
release = threading.Event()
statuses: Queue[int] = Queue()

def block_before_fork() -> None:
    if threading.current_thread().name == "fork-owner":
        entered.set()
        if not release.wait(timeout=5):
            raise RuntimeError("fork owner was not released")

os.register_at_fork(before=block_before_fork)

if sys.argv[2] == "rejected":
    from typing import TYPE_CHECKING

    if TYPE_CHECKING:
        from _typeshed import Unused

    def reject_add_hook(event: str, _args: Unused) -> None:
        if event == "sys.addaudithook":
            raise RuntimeError

    sys.addaudithook(reject_add_hook)

from filelock import FileLock

FileLock(sys.argv[1], is_singleton=False)

def fork_once() -> None:
    child_pid = os.fork()
    if child_pid == 0:
        os._exit(0)
    _, status = os.waitpid(child_pid, 0)
    statuses.put(os.waitstatus_to_exitcode(status))

owner = threading.Thread(target=fork_once, name="fork-owner")
owner.start()
if not entered.wait(timeout=5):
    sys.exit(1)
try:
    child_pid = os.fork()
except RuntimeError as exception:
    second_status = 0 if "unsafe while filelock is changing descriptor ownership" in str(exception) else 1
else:
    if child_pid == 0:
        os._exit(0)
    _, status = os.waitpid(child_pid, 0)
    second_status = os.waitstatus_to_exitcode(status)
release.set()
owner.join(timeout=5)
if second_status != 0 or owner.is_alive() or statuses.get(timeout=1) != 0:
    sys.exit(2)
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path / "parent.lock"), audit_mode],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode == 0, result.stderr


@NEEDS_FORK  # pragma: needs fork
@FORK_WARNING
def test_fork_interrupted_while_waiting_for_a_transition_reopens_admission(tmp_path: Path) -> None:
    # An exception in a before-fork callback does not prevent CPython from forking.
    script = """
from __future__ import annotations

import os
import signal
import sys
import threading
from unittest import mock

from filelock import FileLock

entered = threading.Event()
release = threading.Event()
real_open = os.open

def slow_open(*args: object, **kwargs: object) -> int:
    if threading.current_thread().name == "slow-open":
        entered.set()
        release.wait(timeout=5)
    return real_open(*args, **kwargs)

def use_lock(path: str) -> None:
    with FileLock(path).acquire(timeout=0):
        pass

def fork_once() -> int:
    child_pid = os.fork()
    if child_pid == 0:
        os._exit(1 if held.is_locked else 0)
    _, status = os.waitpid(child_pid, 0)
    return os.waitstatus_to_exitcode(status)

def interrupt(_signum: int, _frame: object) -> None:
    raise KeyboardInterrupt

held = FileLock(sys.argv[3])
held.acquire()
with mock.patch("os.open", side_effect=slow_open):
    slow = threading.Thread(target=use_lock, args=(sys.argv[1],), name="slow-open", daemon=True)
    slow.start()
    if not entered.wait(timeout=5):
        sys.exit(1)
    signal.signal(signal.SIGALRM, interrupt)
    signal.setitimer(signal.ITIMER_REAL, 0.2)
    interrupted_status = fork_once()
    release.set()
    slow.join(timeout=5)
later = threading.Thread(target=use_lock, args=(sys.argv[2],), daemon=True)
later.start()
later.join(timeout=5)
if interrupted_status != 0 or slow.is_alive() or later.is_alive():
    sys.exit(2)
if fork_once() != 0:
    sys.exit(3)
"""
    result = subprocess.run(
        [sys.executable, "-c", script, *(str(tmp_path / name) for name in ("slow.lock", "later.lock", "held.lock"))],
        check=False,
        capture_output=True,
        text=True,
        timeout=20,
    )

    assert result.returncode == 0, result.stderr


@NEEDS_FORK  # pragma: needs fork
@FORK_WARNING
def test_fork_interrupted_while_taking_the_gate_reports_no_after_hook_error(tmp_path: Path) -> None:
    # CPython forks anyway when the before hook raises, and the after hook then finds no depth stored for the thread.
    script = """
from __future__ import annotations

import os
import sys
import threading
from types import FrameType

from filelock import FileLock
from filelock._core._fork import _pin_fork_objects

fired = False

def hook(frame: FrameType, event: str, _arg: object) -> None:
    global fired
    if (
        event == "call"
        and frame.f_code is threading.Condition.__enter__.__code__
        and frame.f_back is not None
        and frame.f_back.f_code is _pin_fork_objects.__code__
        and not fired
    ):
        fired = True
        raise KeyboardInterrupt

held = FileLock(sys.argv[1])
held.acquire()
sys.setprofile(hook)
try:
    child_pid = os.fork()
finally:
    sys.setprofile(None)
if child_pid == 0:
    os._exit(0)
os.waitpid(child_pid, 0)
later = threading.Thread(target=lambda: FileLock(sys.argv[2]).acquire(timeout=0), daemon=True)
later.start()
later.join(timeout=5)
sys.exit(0 if fired and not later.is_alive() else 1)
"""
    result = subprocess.run(
        [sys.executable, "-c", script, *(str(tmp_path / name) for name in ("held.lock", "later.lock"))],
        check=False,
        capture_output=True,
        text=True,
        timeout=20,
    )

    assert result.returncode == 0, result.stderr
    assert "_resume_parent_after_fork" not in result.stderr


def test_fork_exec_from_another_thread_remains_available(tmp_path: Path) -> None:
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()

    class BlockingTransitionLock(BaseFileLock):
        @override
        def _acquire(self) -> None:
            entered.set()
            release.wait(timeout=5)
            self._context.lock_file_fd = None

        _release = _acquire

    def acquire() -> None:
        try:
            BlockingTransitionLock(str(tmp_path / "transition.lock"), is_singleton=False).acquire(timeout=0)
        except Timeout:
            finished.set()

    worker = threading.Thread(target=acquire)
    worker.start()
    assert entered.wait(timeout=5)
    sys.audit("_posixsubprocess.fork_exec", (), (), None)
    result = subprocess.run([sys.executable, "-c", ""], check=False, timeout=5)
    release.set()
    worker.join(timeout=5)

    assert (result.returncode, finished.is_set(), worker.is_alive()) == (0, True, False)


@NEEDS_FORK
def test_fork_transition_finished_on_another_thread_releases_the_entering_thread() -> None:  # pragma: needs fork
    # A generator suspended inside a transition can be finalized on any thread.
    transition = fork_transition()
    transition.__enter__()  # ruff:ignore[unnecessary-dunder-call]  # entered and left on different threads
    with ThreadPoolExecutor(max_workers=1) as pool:
        pool.submit(transition.__exit__, None, None, None).result()
    assert threading.get_ident() not in _FORK_STATE.transitions
