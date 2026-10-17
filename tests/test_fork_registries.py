from __future__ import annotations

import gc
import os
import signal
import subprocess  # ruff:ignore[suspicious-subprocess-import]  # interpreter exit exercises the atexit registry
import sys
import threading
import weakref
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, NoReturn

import pytest

from filelock import BaseFileLock, SoftReadWriteLock
from filelock._soft_rw._protocol import GenerationLog
from filelock._soft_rw._storage import OsFiles
from tests.capability_marks import NEEDS_CLASS_COLLECTION, NEEDS_FORK
from tests.fork_helpers import exit_child, fork_process

if TYPE_CHECKING:
    from pathlib import Path


@NEEDS_FORK
def test_equal_unhashable_locks_reset_independently(tmp_path: Path) -> None:  # pragma: win32 no cover
    @dataclass(eq=True, init=False)  # pragma: win32 no cover
    class EqualLock(BaseFileLock):  # pragma: win32 no cover
        def _acquire(self) -> None:  # pragma: win32 no cover
            self._context.lock_file_fd = os.open(self.lock_file, os.O_CREAT | os.O_RDWR, 0o600)

        def _release(self) -> None:  # pragma: win32 no cover
            os.close(self._context.lock_file_fd if self._context.lock_file_fd is not None else -1)
            self._context.lock_file_fd = None

    first = EqualLock(str(tmp_path / "first.lock"), is_singleton=False)
    second = EqualLock(str(tmp_path / "second.lock"), is_singleton=False)
    first.acquire()
    second.acquire()

    def reset_child() -> NoReturn:
        state = first.is_locked, first.lock_counter, second.is_locked, second.lock_counter
        exit_child(0 if state == (False, 0, False, 0) else 1)

    child_pid = fork_process(reset_child)
    _, status = os.waitpid(child_pid, 0)
    first.release()
    second.release()

    assert os.waitstatus_to_exitcode(status) == 0


@NEEDS_FORK
def test_soft_read_write_subclass_singleton_is_fresh_after_fork(tmp_path: Path) -> None:  # pragma: win32 no cover
    class CustomLock(SoftReadWriteLock):  # pragma: win32 no cover
        pass

    parent_lock = CustomLock(tmp_path / "test.lock")

    def reset_child() -> NoReturn:
        child_lock = CustomLock(parent_lock.lock_file)
        fresh = type(child_lock) is CustomLock and child_lock is not parent_lock
        child_lock.close()
        exit_child(0 if fresh else 1)

    try:
        child_pid = fork_process(reset_child)
        _, status = os.waitpid(child_pid, 0)
        assert os.waitstatus_to_exitcode(status) == 0
        assert CustomLock(parent_lock.lock_file) is parent_lock
    finally:
        parent_lock.close()


@NEEDS_FORK
@pytest.mark.filterwarnings("ignore:.*multi-threaded, use of fork.*:DeprecationWarning")
def test_soft_read_write_subclass_construction_state_is_reset_after_fork(tmp_path: Path) -> None:  # pragma: needs fork
    entered, release = threading.Event(), threading.Event()
    parent_pid = os.getpid()
    path = str(tmp_path / "construction.lock")

    class CustomLock(SoftReadWriteLock):
        def __init__(self, *args: Any, **kwargs: Any) -> None:  # ruff:ignore[any-type]  # forwards the public constructor unchanged
            if type(self) is CustomLock and os.getpid() == parent_pid:
                entered.set()
                assert release.wait(timeout=10)
            super().__init__(*args, **kwargs)

    class ChildLock(CustomLock):
        pass

    class SiblingLock(SoftReadWriteLock):
        pass

    lock_types = (SoftReadWriteLock, ChildLock, SiblingLock)
    parents = [lock_type(path) for lock_type in lock_types]

    def reset_child() -> NoReturn:
        assert sys.platform != "win32"  # NEEDS_FORK enforces this; narrow signal's platform-specific API for ty.
        # A stale mutex or construction marker must fail within the parent's wait budget.
        signal.alarm(5)
        status = 1
        try:
            for lock_type in (*lock_types, CustomLock):
                with closing(lock_type(path)) as child:
                    assert type(child) is lock_type
                    assert all(child is not parent for parent in parents)
                    assert lock_type(path) is child
            status = 0
        finally:
            exit_child(status)

    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(CustomLock, path)
            try:
                assert entered.wait(timeout=5)
                child_pid = fork_process(reset_child)
                _, status = os.waitpid(child_pid, 0)
                assert os.waitstatus_to_exitcode(status) == 0
            finally:
                release.set()
                with closing(pending.result(timeout=5)) as parent:
                    assert CustomLock(path) is parent
        assert all(lock_type(path) is parent for lock_type, parent in zip(lock_types, parents, strict=True))
    finally:
        for parent in parents:
            parent.close()


@pytest.mark.requires_hard_links
def test_equal_unhashable_soft_read_write_locks_survive_atexit_registry(
    tmp_path: Path,
) -> None:  # pragma: needs hard-link
    script = """
from __future__ import annotations

import sys
from dataclasses import dataclass

from filelock import SoftReadWriteLock

@dataclass(eq=True, init=False)
class EqualLock(SoftReadWriteLock):
    pass

locks = [EqualLock(path, is_singleton=False, heartbeat_interval=1, stale_threshold=30) for path in sys.argv[1:]]
for lock in locks:
    lock.acquire_write()
"""
    paths = [tmp_path / "first.lock", tmp_path / "second.lock"]
    result = subprocess.run(
        [sys.executable, "-c", script, *(str(path) for path in paths)],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )

    writers = [GenerationLog(OsFiles(str(path)), str(path), f"{path}.rw").latest().writer for path in paths]
    assert (result.returncode, writers) == (0, [None, None])


@NEEDS_CLASS_COLLECTION
def test_dynamic_lock_class_can_be_collected() -> None:
    class EphemeralLock(BaseFileLock):
        _acquire = _release = BaseFileLock._acquire

    class_ref = weakref.ref(EphemeralLock)
    del EphemeralLock
    gc.collect()

    assert class_ref() is None
