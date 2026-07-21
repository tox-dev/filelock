from __future__ import annotations

import gc
import os
import subprocess  # ruff:ignore[suspicious-subprocess-import]  # interpreter exit exercises the atexit registry
import sys
import weakref
from dataclasses import dataclass
from typing import TYPE_CHECKING, NoReturn

from capability_marks import NEEDS_CLASS_COLLECTION, NEEDS_FORK
from fork_helpers import exit_child, fork_process

from filelock import BaseFileLock

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


def test_equal_unhashable_soft_read_write_locks_survive_atexit_registry(tmp_path: Path) -> None:
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

    assert (result.returncode, [path.with_name(f"{path.name}.write").exists() for path in paths]) == (0, [False, False])


@NEEDS_CLASS_COLLECTION
def test_dynamic_lock_class_can_be_collected() -> None:
    class EphemeralLock(BaseFileLock):
        _acquire = _release = BaseFileLock._acquire

    class_ref = weakref.ref(EphemeralLock)
    del EphemeralLock
    gc.collect()

    assert class_ref() is None
