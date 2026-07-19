from __future__ import annotations

import os
import subprocess  # ruff:ignore[suspicious-subprocess-import]  # isolated interpreters control at-fork registration order
import sys
import threading
from typing import TYPE_CHECKING, Final, Literal

import pytest
from fork_helpers import exit_child, fork_process

from filelock import BaseFileLock, Timeout, has_fcntl

if TYPE_CHECKING:
    from pathlib import Path

_REQUIRES_FORK: Final[pytest.MarkDecorator] = pytest.mark.skipif(
    not (hasattr(os, "fork") and hasattr(os, "register_at_fork")), reason="os.fork and os.register_at_fork required"
)
_FORK_WARNING: Final[pytest.MarkDecorator] = pytest.mark.filterwarnings(
    "ignore:.*multi-threaded, use of fork.*:DeprecationWarning"
)


@_REQUIRES_FORK  # pragma: needs fork
@_FORK_WARNING
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


@_REQUIRES_FORK  # pragma: needs fork
@_FORK_WARNING
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
    # Audit arguments mix interpreter-owned types; object is their only accurate common type.
    def reject_add_hook(event: str, _args: tuple[object, ...]) -> None:
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


@_REQUIRES_FORK  # pragma: needs fork
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
        def _acquire(self) -> None:
            self._context.lock_file_fd = None
            if event == "_posixsubprocess.fork_exec":
                sys.audit(event, (), (), None)
            else:
                sys.audit(event)

        _release = _acquire

    with pytest.raises(RuntimeError, match="unsafe while filelock is changing descriptor ownership"):
        AuditedLock(str(tmp_path / "audit.lock"), is_singleton=False).acquire(timeout=0)


@_REQUIRES_FORK  # pragma: needs fork
@_FORK_WARNING
@pytest.mark.parametrize(
    "fork_mode",
    [
        pytest.param("rejected-audit", id="rejected-audit-hook"),
        pytest.param(
            "fork1",
            marks=pytest.mark.skipif(not hasattr(os, "fork1"), reason="os.fork1 required"),
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
    # Audit arguments are heterogeneous interpreter-owned values, so object is their only accurate common type.
    def reject_filelock_hook(event: str, _args: tuple[object, ...]) -> None:
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


@_REQUIRES_FORK  # pragma: needs fork
@_FORK_WARNING
@pytest.mark.skipif(not has_fcntl, reason="fcntl.flock required")
@pytest.mark.skipif(sys.implementation.name != "cpython", reason="CPython fcntl audit event required")
@pytest.mark.parametrize(
    "fork_mode",
    [
        pytest.param("rejected-audit", id="rejected-audit-hook"),
        pytest.param(
            "fork1",
            marks=pytest.mark.skipif(not hasattr(os, "fork1"), reason="os.fork1 required"),
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

# Audit arguments are heterogeneous interpreter-owned values, so object is their only accurate common type.
def audit_hook(event: str, args: tuple[object, ...]) -> None:
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


@_REQUIRES_FORK  # pragma: needs fork
@_FORK_WARNING
def test_child_unwinds_pre_fork_transition_before_new_acquire(tmp_path: Path) -> None:
    script = """
from __future__ import annotations

import os
import sys
from errno import EBADF

# Audit arguments are heterogeneous interpreter-owned values, so object is their only accurate common type.
def reject_filelock_hook(event: str, _args: tuple[object, ...]) -> None:
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


@_REQUIRES_FORK  # pragma: needs fork
@_FORK_WARNING
def test_overlapping_coroutine_transitions_close_every_pending_descriptor(tmp_path: Path) -> None:
    script = """
from __future__ import annotations

import asyncio
import os
import sys
from errno import EBADF

# Audit arguments are heterogeneous interpreter-owned values, so object is their only accurate common type.
def reject_filelock_hook(event: str, _args: tuple[object, ...]) -> None:
    if event == "sys.addaudithook":
        raise RuntimeError

sys.addaudithook(reject_filelock_hook)

from filelock import BaseAsyncFileLock

both_pending = asyncio.Event()
allow_return = asyncio.Event()
descriptors: list[int] = []
child_status = -1

class OverlapLock(BaseAsyncFileLock):
    async def _acquire(self) -> None:  # ty: ignore[invalid-method-override]  # coroutine backends are supported
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

    async def _release(self) -> None:  # ty: ignore[invalid-method-override]  # coroutine backends are supported
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


@_REQUIRES_FORK  # pragma: needs fork
@_FORK_WARNING
def test_pending_descriptor_with_unknown_identity_is_preserved_in_child(tmp_path: Path) -> None:
    script = """
from __future__ import annotations

import os
import sys

# Audit arguments are heterogeneous interpreter-owned values, so object is their only accurate common type.
def reject_filelock_hook(event: str, _args: tuple[object, ...]) -> None:
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


@_REQUIRES_FORK  # pragma: needs fork
@_FORK_WARNING
def test_child_replaces_parameter_model_mutex_held_during_construction(tmp_path: Path) -> None:
    script = """
from __future__ import annotations

import inspect
import os
import sys
import threading
import warnings
from collections.abc import Callable
from queue import Queue

warnings.filterwarnings("ignore", message=".*multi-threaded, use of fork.*", category=DeprecationWarning)

# Audit arguments are heterogeneous interpreter-owned values, so object is their only accurate common type.
def reject_filelock_hook(event: str, _args: tuple[object, ...]) -> None:
    if event == "sys.addaudithook":
        raise RuntimeError

sys.addaudithook(reject_filelock_hook)

from filelock import BaseFileLock

parent_pid = os.getpid()
forked = False
paused = threading.Event()
release = threading.Event()
children: Queue[int] = Queue()
real_signature = inspect.signature

class BlockingLock(BaseFileLock):
    def _acquire(self) -> None:
        self._context.lock_file_fd = None

    def _release(self) -> None:
        self._context.lock_file_fd = None

class ChildLock(BaseFileLock):
    def _acquire(self) -> None:
        self._context.lock_file_fd = None

    def _release(self) -> None:
        self._context.lock_file_fd = None

def blocking_signature(target: Callable[..., BaseFileLock | None]) -> inspect.Signature:
    global forked
    if target is BlockingLock.__init__ and not forked:
        forked = True
        child_pid = os.fork()
        if child_pid == 0:
            ChildLock(sys.argv[2], is_singleton=False)
            os._exit(0)
        children.put(child_pid)
        paused.set()
        if not release.wait(timeout=5):
            raise RuntimeError("parent construction was not released")
    return real_signature(target)

inspect.signature = blocking_signature

def construct() -> None:
    BlockingLock(sys.argv[1], is_singleton=False)

worker = threading.Thread(target=construct)
worker.start()
if not paused.wait(timeout=5):
    sys.exit(1)
child_pid = children.get(timeout=1)
_, status = os.waitpid(child_pid, 0)
release.set()
worker.join(timeout=5)
if os.waitstatus_to_exitcode(status) != 0 or worker.is_alive() or os.getpid() != parent_pid:
    sys.exit(2)
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path / "parent.lock"), str(tmp_path / "child.lock")],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert (result.returncode, result.stderr) == (0, "")


@_REQUIRES_FORK  # pragma: needs fork
@_FORK_WARNING
def test_registration_transition_completes_after_fork_closes_admission(tmp_path: Path) -> None:
    script = """
from __future__ import annotations

import os
import sys
import threading
import warnings
from queue import Queue

warnings.filterwarnings("ignore", message=".*multi-threaded, use of fork.*", category=DeprecationWarning)

admission_waiting = threading.Event()
real_condition_wait = threading.Condition.wait

def observed_wait(condition: threading.Condition, timeout: float | None = None) -> bool:
    if threading.current_thread().name == "forker":
        admission_waiting.set()
    return real_condition_wait(condition, timeout)

threading.Condition.wait = observed_wait

from filelock import BaseFileLock

entered = threading.Event()
release = threading.Event()
failures: Queue[str] = Queue()
fork_status: Queue[int] = Queue()

class BlockingLock(BaseFileLock):
    def _acquire(self) -> None:
        self._context.lock_file_fd = os.open(self.lock_file, os.O_CREAT | os.O_RDWR, 0o600)
        entered.set()
        if not release.wait(timeout=5):
            raise RuntimeError("backend acquisition was not released")

    def _release(self) -> None:
        os.close(self._context.lock_file_fd if self._context.lock_file_fd is not None else -1)
        self._context.lock_file_fd = None

lock = BlockingLock(sys.argv[1], is_singleton=False)

def acquire() -> None:
    try:
        lock.acquire(timeout=0)
    except BaseException as exception:
        failures.put(f"acquire: {exception!r}")

def fork() -> None:
    try:
        child_pid = os.fork()
        if child_pid == 0:
            os._exit(0)
        _, status = os.waitpid(child_pid, 0)
        fork_status.put(os.waitstatus_to_exitcode(status))
    except BaseException as exception:
        failures.put(f"fork: {exception!r}")

worker = threading.Thread(target=acquire, name="acquirer")
worker.start()
if not entered.wait(timeout=5):
    sys.exit(1)
forker = threading.Thread(target=fork, name="forker")
forker.start()
if not admission_waiting.wait(timeout=5):
    sys.exit(2)
release.set()
worker.join(timeout=5)
forker.join(timeout=5)
if worker.is_alive() or forker.is_alive() or not failures.empty() or fork_status.get(timeout=1) != 0:
    sys.exit(3)
lock.release()
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path / "admission.lock")],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert (result.returncode, result.stderr) == (0, "")


@_REQUIRES_FORK  # pragma: needs fork
@_FORK_WARNING
@pytest.mark.skipif(not has_fcntl, reason="fcntl.flock required")
def test_cancelled_async_acquire_unregisters_reused_descriptor(tmp_path: Path) -> None:
    script = """
from __future__ import annotations

import asyncio
import os
import sys
import threading
import warnings

from filelock import AsyncFileLock

warnings.filterwarnings("ignore", message=".*multi-threaded, use of fork.*", category=DeprecationWarning)

async def main() -> int:
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
        return 1
    if lock.is_locked or lock.lock_counter != 0:
        return 2

    path_stat = os.stat(sys.argv[1])
    if (path_stat.st_dev, path_stat.st_ino) != identity:
        return 3

    occupant = os.open(os.devnull, os.O_RDONLY)
    if occupant != descriptor:
        os.dup2(occupant, descriptor)
    source = os.open(sys.argv[1], os.O_RDWR)
    if source == descriptor:
        return 4
    if (source_stat := os.fstat(source)).st_dev != identity[0] or source_stat.st_ino != identity[1]:
        return 5
    os.dup2(source, descriptor)
    os.close(source)
    if occupant != descriptor:
        os.close(occupant)

    child_pid = os.fork()
    if child_pid == 0:
        try:
            replacement_stat = os.fstat(descriptor)
        except OSError:
            os._exit(1)
        os._exit(0 if (replacement_stat.st_dev, replacement_stat.st_ino) == identity else 2)
    _, status = os.waitpid(child_pid, 0)
    os.close(descriptor)
    return os.waitstatus_to_exitcode(status)

raise SystemExit(asyncio.run(main()))
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path / "canceled.lock")],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert (result.returncode, result.stderr) == (0, "")


@_REQUIRES_FORK  # pragma: needs fork
@_FORK_WARNING
def test_parent_callback_rejects_reentrant_singleton_construction(tmp_path: Path) -> None:
    script = """
from __future__ import annotations

import os
import sys
from collections.abc import Callable

active_constructor: Callable[[], BaseFileLock | SoftReadWriteLock] | None = None
callback_errors: list[str] = []
callback_instances: list[BaseFileLock | SoftReadWriteLock] = []

def construct_in_parent_callback() -> None:
    if active_constructor is None:
        return
    try:
        callback_instances.append(active_constructor())
    except RuntimeError as exception:
        callback_errors.append(str(exception))

os.register_at_fork(after_in_parent=construct_in_parent_callback)

from filelock import BaseFileLock, SoftReadWriteLock

fork_in_progress = False
children: list[int] = []

def fork_once() -> None:
    global fork_in_progress
    if fork_in_progress:
        return
    fork_in_progress = True
    child_pid = os.fork()
    if child_pid == 0:
        os._exit(0)
    children.append(child_pid)
    fork_in_progress = False

class ForkingFileLock(BaseFileLock):
    def __init__(
        self, lock_file: str, timeout: float = -1, thread_local: bool = True, *, is_singleton: bool = False
    ) -> None:
        fork_once()
        super().__init__(lock_file, timeout, thread_local=thread_local, is_singleton=is_singleton)

    def _acquire(self) -> None:
        self._context.lock_file_fd = None

    def _release(self) -> None:
        self._context.lock_file_fd = None

class ForkingSoftReadWriteLock(SoftReadWriteLock):
    def __init__(
        self,
        lock_file: str,
        timeout: float = -1,
        *,
        blocking: bool = True,
        is_singleton: bool = True,
        heartbeat_interval: float = 30.0,
        stale_threshold: float | None = None,
        poll_interval: float = 0.25,
    ) -> None:
        fork_once()
        super().__init__(
            lock_file,
            timeout,
            blocking=blocking,
            is_singleton=is_singleton,
            heartbeat_interval=heartbeat_interval,
            stale_threshold=stale_threshold,
            poll_interval=poll_interval,
        )

file_path, read_write_path = sys.argv[1:]
active_constructor = lambda: ForkingFileLock(file_path, thread_local=False, is_singleton=True)
file_lock = ForkingFileLock(file_path, thread_local=False, is_singleton=True)
active_constructor = lambda: ForkingSoftReadWriteLock(read_write_path, is_singleton=True)
read_write_lock = ForkingSoftReadWriteLock(read_write_path, is_singleton=True)
active_constructor = None

statuses = [os.waitstatus_to_exitcode(os.waitpid(child_pid, 0)[1]) for child_pid in children]
if (
    statuses != [0, 0]
    or callback_instances
    or len(callback_errors) != 2
    or not all("Singleton lock construction is already active" in error for error in callback_errors)
    or ForkingFileLock(file_path, thread_local=False, is_singleton=True) is not file_lock
    or ForkingSoftReadWriteLock(read_write_path, is_singleton=True) is not read_write_lock
):
    sys.exit(1)
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path / "file.lock"), str(tmp_path / "read-write.lock")],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert (result.returncode, result.stderr) == (0, "")


@_REQUIRES_FORK  # pragma: needs fork
@_FORK_WARNING
def test_singleton_construction_crossing_fork_does_not_poison_child_cache(tmp_path: Path) -> None:
    script = """
from __future__ import annotations

import os
import sys

from filelock import BaseFileLock

parent_pid = os.getpid()
forked = False
children: list[int] = []

class ForkingLock(BaseFileLock):
    def __init__(self, lock_file: str, thread_local: bool = True, *, is_singleton: bool = False) -> None:
        global forked
        if not forked:
            forked = True
            child_pid = os.fork()
            if child_pid != 0:
                children.append(child_pid)
        super().__init__(lock_file, thread_local=thread_local, is_singleton=is_singleton)

    def _acquire(self) -> None:
        self._context.lock_file_fd = None

    def _release(self) -> None:
        self._context.lock_file_fd = None

try:
    parent_lock = ForkingLock(sys.argv[1], thread_local=False, is_singleton=True)
except RuntimeError as exception:
    if os.getpid() == parent_pid or "Lock construction cannot continue after fork" not in str(exception):
        raise
    child_lock = ForkingLock(sys.argv[1], thread_local=False, is_singleton=True)
    raise SystemExit(0 if ForkingLock(sys.argv[1], thread_local=False, is_singleton=True) is child_lock else 1)

_, status = os.waitpid(children[0], 0)
if (
    os.waitstatus_to_exitcode(status) != 0
    or ForkingLock(sys.argv[1], thread_local=False, is_singleton=True) is not parent_lock
):
    sys.exit(2)
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path / "singleton.lock")],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert (result.returncode, result.stderr) == (0, "")


@_REQUIRES_FORK  # pragma: needs fork
@_FORK_WARNING
def test_soft_read_write_construction_crossing_fork_does_not_poison_child_cache(tmp_path: Path) -> None:
    script = """
from __future__ import annotations

import os
import sys

from filelock import SoftReadWriteLock

parent_pid = os.getpid()
forked = False
children: list[int] = []

class ForkingLock(SoftReadWriteLock):
    def __init__(
        self,
        lock_file: str,
        timeout: float = -1,
        *,
        blocking: bool = True,
        is_singleton: bool = True,
        heartbeat_interval: float = 30.0,
        stale_threshold: float | None = None,
        poll_interval: float = 0.25,
    ) -> None:
        global forked
        if not forked:
            forked = True
            child_pid = os.fork()
            if child_pid != 0:
                children.append(child_pid)
        super().__init__(
            lock_file,
            timeout,
            blocking=blocking,
            is_singleton=is_singleton,
            heartbeat_interval=heartbeat_interval,
            stale_threshold=stale_threshold,
            poll_interval=poll_interval,
        )

try:
    parent_lock = ForkingLock(sys.argv[1], is_singleton=True)
except RuntimeError as exception:
    if os.getpid() == parent_pid or "Lock construction cannot continue after fork" not in str(exception):
        raise
    child_lock = ForkingLock(sys.argv[1], is_singleton=True)
    child_cached = ForkingLock(sys.argv[1], is_singleton=True)
    child_lock.close()
    raise SystemExit(0 if child_cached is child_lock else 1)

_, status = os.waitpid(children[0], 0)
parent_cached = ForkingLock(sys.argv[1], is_singleton=True)
parent_lock.close()
if os.waitstatus_to_exitcode(status) != 0 or parent_cached is not parent_lock:
    sys.exit(2)
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path / "singleton.lock")],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert (result.returncode, result.stderr) == (0, "")


def test_fork_exec_from_another_thread_remains_available(tmp_path: Path) -> None:
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()

    class BlockingTransitionLock(BaseFileLock):
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


@_REQUIRES_FORK  # pragma: needs fork
@_FORK_WARNING
def test_child_replaces_singleton_mutex_held_by_vanished_thread(tmp_path: Path) -> None:
    entered, release = threading.Event(), threading.Event()
    parent_pid = os.getpid()

    class BlockingLock(BaseFileLock):
        def __init__(self, lock_file: str, *, is_singleton: bool = False) -> None:
            if os.getpid() == parent_pid:
                entered.set()
                release.wait(timeout=5)
            super().__init__(lock_file, thread_local=False, is_singleton=is_singleton)

        def _acquire(self) -> None:
            self._context.lock_file_fd = os.open(self.lock_file, os.O_CREAT | os.O_RDWR, 0o600)

        def _release(self) -> None:
            os.close(self._context.lock_file_fd if self._context.lock_file_fd is not None else -1)
            self._context.lock_file_fd = None

    worker = threading.Thread(target=BlockingLock, args=(str(tmp_path / "mutex.lock"),), kwargs={"is_singleton": True})
    worker.start()
    assert entered.wait(timeout=5)

    if (child_pid := fork_process()) == 0:  # pragma: forked child
        child_lock = BlockingLock(str(tmp_path / "mutex.lock"), is_singleton=True)
        child_lock.acquire(timeout=0)
        child_lock.release()
        exit_child(0)
    _, status = os.waitpid(child_pid, 0)
    release.set()
    worker.join(timeout=5)

    assert (os.waitstatus_to_exitcode(status), worker.is_alive()) == (0, False)
