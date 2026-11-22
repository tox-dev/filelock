from __future__ import annotations

import subprocess  # ruff:ignore[suspicious-subprocess-import]  # isolated interpreters control at-fork registration order
import sys
from typing import TYPE_CHECKING

import pytest

from filelock import has_fcntl
from tests.capability_marks import (
    NEEDS_FORK,
    NEEDS_REGISTER_AT_FORK,
)

if TYPE_CHECKING:
    from pathlib import Path

from tests.core.fork_probes import FORK_WARNING


@NEEDS_FORK  # pragma: needs fork
@FORK_WARNING
def test_child_unwinds_pre_fork_transition_before_new_acquire(tmp_path: Path) -> None:
    script = """
from __future__ import annotations

import os
import sys
from errno import EBADF

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from _typeshed import Unused

def reject_filelock_hook(event: str, _args: Unused) -> None:
    if event == "sys.addaudithook":
        raise RuntimeError

sys.addaudithook(reject_filelock_hook)

from filelock import BaseFileLock

class ForkingLock(BaseFileLock):
    child_pid = -1

    def _acquire(self) -> None:
        self._context.lock_file_fd = os.open(self.lock_file, os.O_CREAT | os.O_RDWR, 0o600)
        self.child_pid = os.fork()

    def _release(self) -> None:
        os.close(self._context.lock_file_fd if self._context.lock_file_fd is not None else -1)
        self._context.lock_file_fd = None

first = ForkingLock(sys.argv[1], is_singleton=False)
try:
    first.acquire()
except RuntimeError as exception:
    if first.child_pid != 0 or "inherited across fork" not in str(exception):
        raise

    class ProbeLock(BaseFileLock):
        child_status = -1

        def _acquire(self) -> None:
            descriptor = self._context.lock_file_fd = os.open(self.lock_file, os.O_CREAT | os.O_RDWR, 0o600)
            child_pid = os.fork()
            if child_pid == 0:
                try:
                    os.fstat(descriptor)
                except OSError as error:
                    os._exit(0 if error.errno == EBADF else 1)
                os._exit(1)
            _, status = os.waitpid(child_pid, 0)
            self.child_status = os.waitstatus_to_exitcode(status)

        def _release(self) -> None:
            os.close(self._context.lock_file_fd if self._context.lock_file_fd is not None else -1)
            self._context.lock_file_fd = None

    probe = ProbeLock(sys.argv[2], is_singleton=False)
    probe.acquire()
    probe.release()
    os._exit(probe.child_status)

_, status = os.waitpid(first.child_pid, 0)
first.release()
raise SystemExit(os.waitstatus_to_exitcode(status))
"""
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            script,
            str(tmp_path / "first.lock"),
            str(tmp_path / "probe.lock"),
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert (result.returncode, result.stderr) == (0, "")


@NEEDS_FORK  # pragma: needs fork
@FORK_WARNING
def test_overlapping_coroutine_transitions_close_every_pending_descriptor(tmp_path: Path) -> None:
    script = """
from __future__ import annotations

import asyncio
import os
import sys
from errno import EBADF

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from _typeshed import Unused

def reject_filelock_hook(event: str, _args: Unused) -> None:
    if event == "sys.addaudithook":
        raise RuntimeError

sys.addaudithook(reject_filelock_hook)

from filelock import BaseAsyncFileLock

both_pending = asyncio.Event()
allow_return = asyncio.Event()
descriptors: list[int] = []
child_status = -1

class OverlapLock(BaseAsyncFileLock):
    async def _acquire(self) -> None:  # ty: ignore[incompatible-method-override]  # coroutine backends are supported
        global child_status
        descriptor = self._context.lock_file_fd = os.open(self.lock_file, os.O_CREAT | os.O_RDWR, 0o600)
        descriptors.append(descriptor)
        if len(descriptors) == 2:
            both_pending.set()
        await both_pending.wait()
        if self.lock_file != sys.argv[1]:
            await allow_return.wait()
            return
        child_pid = os.fork()
        if child_pid == 0:
            for inherited in descriptors:
                try:
                    os.fstat(inherited)
                except OSError as exception:
                    if exception.errno == EBADF:
                        continue
                os._exit(1)
            os._exit(0)
        _, status = os.waitpid(child_pid, 0)
        child_status = os.waitstatus_to_exitcode(status)
        allow_return.set()

    async def _release(self) -> None:  # ty: ignore[incompatible-method-override]  # coroutine backends are supported
        os.close(self._context.lock_file_fd if self._context.lock_file_fd is not None else -1)
        self._context.lock_file_fd = None

async def main() -> None:
    first = OverlapLock(sys.argv[1], thread_local=False, is_singleton=False, run_in_executor=False)
    second = OverlapLock(sys.argv[2], thread_local=False, is_singleton=False, run_in_executor=False)
    await asyncio.gather(first.acquire(timeout=0), second.acquire(timeout=0))
    await asyncio.gather(first.release(), second.release())
    raise SystemExit(child_status)

asyncio.run(main())
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path / "first.lock"), str(tmp_path / "second.lock")],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert (result.returncode, result.stderr) == (0, "")


@NEEDS_FORK  # pragma: needs fork
@FORK_WARNING
def test_pending_descriptor_with_unknown_identity_is_preserved_in_child(tmp_path: Path) -> None:
    script = """
from __future__ import annotations

import os
import sys

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from _typeshed import Unused

def reject_filelock_hook(event: str, _args: Unused) -> None:
    if event == "sys.addaudithook":
        raise RuntimeError

sys.addaudithook(reject_filelock_hook)

from filelock import BaseFileLock

real_fstat = os.fstat

class PendingLock(BaseFileLock):
    child_status = -1

    def _acquire(self) -> None:
        descriptor = os.open(self.lock_file, os.O_CREAT | os.O_RDWR, 0o600)
        self._context.pending_lock_file_fd = descriptor

        def fail_pending_identity(fd: int) -> os.stat_result:
            if fd == descriptor:
                raise OSError("identity unavailable")
            return real_fstat(fd)

        os.fstat = fail_pending_identity
        child_pid = os.fork()
        os.fstat = real_fstat
        if child_pid == 0:
            real_fstat(descriptor)
            raise SystemExit(0)
        _, status = os.waitpid(child_pid, 0)
        self.child_status = os.waitstatus_to_exitcode(status)
        stat_result = real_fstat(descriptor)
        self._context.pending_lock_file_fd = None
        self._context.lock_file_fd = descriptor
        self._context.lock_file_fd_identity = stat_result.st_dev, stat_result.st_ino

    def _release(self) -> None:
        os.close(self._context.lock_file_fd if self._context.lock_file_fd is not None else -1)
        self._context.lock_file_fd = None

lock = PendingLock(sys.argv[1], is_singleton=False)
lock.acquire(timeout=0)
lock.release()
raise SystemExit(lock.child_status)
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path / "pending.lock")],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert (result.returncode, result.stderr) == (0, "")


@NEEDS_REGISTER_AT_FORK  # pragma: needs register-at-fork
@pytest.mark.timeout(30)
def test_registration_transition_completes_after_fork_hook_closes_admission(tmp_path: Path) -> None:
    script = """
from __future__ import annotations

import os
import sys
import threading
from queue import Queue

from filelock import BaseFileLock
from filelock._core._fork import _FORK_STATE, _pin_fork_objects, _resume_parent_after_fork

admission_waiting = threading.Event()
real_condition_wait = threading.Condition.wait

def observed_wait(condition: threading.Condition, timeout: float | None = None) -> bool:
    if condition is _FORK_STATE.gate and threading.current_thread() is threading.main_thread():
        admission_waiting.set()
    return real_condition_wait(condition, timeout)

threading.Condition.wait = observed_wait

entered = threading.Event()
acquired = threading.Event()
release = threading.Event()
failures: Queue[str] = Queue()

class BlockingLock(BaseFileLock):
    def _acquire(self) -> None:
        self._context.lock_file_fd = os.open(self.lock_file, os.O_CREAT | os.O_RDWR, 0o600)
        entered.set()
        if not release.wait(timeout=10):
            raise RuntimeError("backend acquisition was not released")

    def _release(self) -> None:
        os.close(self._context.lock_file_fd if self._context.lock_file_fd is not None else -1)
        self._context.lock_file_fd = None

lock = BlockingLock(sys.argv[1], is_singleton=False)

def acquire() -> None:
    try:
        lock.acquire(timeout=0)
        acquired.set()
    except BaseException as exception:
        failures.put(f"acquire: {exception!r}")

def release_after_admission() -> None:
    try:
        if not admission_waiting.wait(timeout=5):
            failures.put("fork hook did not wait for the active transition")
    finally:
        release.set()

worker = threading.Thread(target=acquire, name="acquirer", daemon=True)
worker.start()
if not entered.wait(timeout=5):
    raise RuntimeError("backend acquisition did not start")
releaser = threading.Thread(target=release_after_admission, name="releaser", daemon=True)
releaser.start()
try:
    _pin_fork_objects()
    if not _FORK_STATE.admission_closed:
        raise RuntimeError("fork hook did not close admission")
finally:
    _resume_parent_after_fork()
worker.join(timeout=5)
releaser.join(timeout=5)
if worker.is_alive() or releaser.is_alive():
    raise RuntimeError("worker or releaser remained blocked")
if not failures.empty():
    raise RuntimeError(failures.get_nowait())
if not acquired.is_set():
    raise RuntimeError("worker did not finish acquiring the lock")
if _FORK_STATE.admission_closed:
    raise RuntimeError("fork hook did not reopen admission")
lock.release()
lock.acquire(timeout=0)
lock.release()
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path / "admission.lock")],
        check=False,
        capture_output=True,
        text=True,
        timeout=20,
    )

    assert (result.returncode, result.stderr) == (0, "")


@NEEDS_FORK  # pragma: needs fork
@FORK_WARNING
@pytest.mark.skipif(not has_fcntl, reason="fcntl.flock required")
def test_cancelled_async_acquire_unregisters_reused_descriptor(tmp_path: Path) -> None:
    script = """
from __future__ import annotations

import asyncio
import faulthandler
import os
import signal
import sys
import threading
import time
import warnings

from filelock import AsyncFileLock

warnings.filterwarnings("ignore", message=".*multi-threaded, use of fork.*", category=DeprecationWarning)

child_pid = 0

def report_stall(_signum: int, _frame: object) -> None:
    # A stuck child keeps the captured pipes open after its parent dies (NetBSD #707, #689).
    faulthandler.dump_traceback(all_threads=True)
    if child_pid:
        os.kill(child_pid, signal.SIGKILL)
    os._exit(7)

faulthandler.enable()
signal.signal(signal.SIGALRM, report_stall)
signal.alarm(15)

async def main() -> tuple[int, tuple[int, int]]:
    callback_started = asyncio.Event()
    finish_callback = threading.Event()
    loop = asyncio.get_running_loop()
    descriptor = -1
    identity = (-1, -1)

    def block_after_acquire(fd: int) -> None:
        nonlocal descriptor, identity
        descriptor = fd
        stat_result = os.fstat(fd)
        identity = stat_result.st_dev, stat_result.st_ino
        loop.call_soon_threadsafe(callback_started.set)
        if not finish_callback.wait(timeout=5):
            raise RuntimeError("acquisition callback was not released")

    lock = AsyncFileLock(sys.argv[1], on_acquired=block_after_acquire)
    acquire_task = asyncio.create_task(lock.acquire())
    await asyncio.wait_for(callback_started.wait(), timeout=5)
    acquire_task.cancel()
    finish_callback.set()
    try:
        await acquire_task
    except asyncio.CancelledError:
        pass
    else:
        raise SystemExit(1)
    if lock.is_locked or lock.lock_counter != 0:
        raise SystemExit(2)
    return descriptor, identity

descriptor, identity = asyncio.run(main())

path_stat = os.stat(sys.argv[1])
if (path_stat.st_dev, path_stat.st_ino) != identity:
    raise SystemExit(3)

occupant = os.open(os.devnull, os.O_RDONLY)
if occupant != descriptor:
    os.dup2(occupant, descriptor)
source = os.open(sys.argv[1], os.O_RDWR)
if source == descriptor:
    raise SystemExit(4)
if (source_stat := os.fstat(source)).st_dev != identity[0] or source_stat.st_ino != identity[1]:
    raise SystemExit(5)
os.dup2(source, descriptor)
os.close(source)
if occupant != descriptor:
    os.close(occupant)

# On NetBSD the OS worker may outlive Thread.join(). Forking before it exits can deadlock the child (#707).
time.sleep(0.5)
if threading.active_count() != 1:
    raise SystemExit(6)

child_pid = os.fork()
if child_pid == 0:
    try:
        replacement_stat = os.fstat(descriptor)
    except OSError:
        os._exit(1)
    os._exit(0 if (replacement_stat.st_dev, replacement_stat.st_ino) == identity else 2)
_, status = os.waitpid(child_pid, 0)
signal.alarm(0)
os.close(descriptor)
raise SystemExit(os.waitstatus_to_exitcode(status))
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path / "canceled.lock")],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert (result.returncode, result.stderr) == (0, "")
