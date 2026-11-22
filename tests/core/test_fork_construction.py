from __future__ import annotations

import os
import subprocess  # ruff:ignore[suspicious-subprocess-import]  # isolated interpreters control at-fork registration order
import sys
import threading
from typing import TYPE_CHECKING, NoReturn

from filelock import BaseFileLock
from filelock._core import override
from tests.capability_marks import (
    NEEDS_FORK,
)
from tests.fork_helpers import exit_child, fork_process

if TYPE_CHECKING:
    from pathlib import Path

from tests.core.fork_probes import FORK_WARNING


@NEEDS_FORK  # pragma: needs fork
@FORK_WARNING
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

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from _typeshed import Unused

def reject_filelock_hook(event: str, _args: Unused) -> None:
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


@NEEDS_FORK  # pragma: needs fork
@FORK_WARNING
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


@NEEDS_FORK  # pragma: needs fork
@FORK_WARNING
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


@NEEDS_FORK  # pragma: needs fork
@FORK_WARNING
def test_soft_read_write_construction_crossing_fork_does_not_poison_child_cache(
    tmp_path: Path,
) -> None:
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


@NEEDS_FORK  # pragma: needs fork
@FORK_WARNING
def test_child_replaces_singleton_mutex_held_by_vanished_thread(tmp_path: Path) -> None:
    entered, release = threading.Event(), threading.Event()
    parent_pid = os.getpid()

    class BlockingLock(BaseFileLock):
        def __init__(self, lock_file: str, *, is_singleton: bool = False) -> None:
            if os.getpid() == parent_pid:
                entered.set()
                release.wait(timeout=5)
            super().__init__(lock_file, thread_local=False, is_singleton=is_singleton)

        @override
        def _acquire(self) -> None:
            self._context.lock_file_fd = os.open(self.lock_file, os.O_CREAT | os.O_RDWR, 0o600)

        @override
        def _release(self) -> None:
            os.close(self._context.lock_file_fd if self._context.lock_file_fd is not None else -1)
            self._context.lock_file_fd = None

    worker = threading.Thread(target=BlockingLock, args=(str(tmp_path / "mutex.lock"),), kwargs={"is_singleton": True})
    worker.start()
    assert entered.wait(timeout=5)

    def child() -> NoReturn:  # pragma: forked child
        child_lock = BlockingLock(str(tmp_path / "mutex.lock"), is_singleton=True)
        child_lock.acquire(timeout=0)
        child_lock.release()
        exit_child(0)

    _, status = os.waitpid(fork_process(child), 0)
    release.set()
    worker.join(timeout=5)

    assert (os.waitstatus_to_exitcode(status), worker.is_alive()) == (0, False)
