from __future__ import annotations

import asyncio
import gc
import os
import subprocess  # ruff:ignore[suspicious-subprocess-import]  # fresh interpreter must register the callback before importing filelock
import sys
import threading
from typing import TYPE_CHECKING, Final, Literal, NoReturn, cast

import pytest

from filelock import (
    AsyncFileLock,
    AsyncSoftFileLock,
    BaseAsyncFileLock,
    BaseFileLock,
    FileLock,
    SoftFileLock,
    SoftReadWriteLock,
    Timeout,
)
from filelock._core import override
from tests.capability_marks import NEEDS_FORK
from tests.fork_helpers import exit_child, fork_process

if TYPE_CHECKING:
    from pathlib import Path

    from pytest_mock import MockerFixture

from tests.core.fork_probes import FORK_WARNING, cleanup_child_hold, probe_lock


@NEEDS_FORK  # pragma: needs fork
@FORK_WARNING
@pytest.mark.parametrize(
    ("lock_type", "action"),
    [
        pytest.param(FileLock, "release", id="native-release"),
        pytest.param(FileLock, "context", id="native-context"),
        pytest.param(FileLock, "collect", id="native-collect"),
        pytest.param(SoftFileLock, "release", id="soft-release"),
        pytest.param(SoftFileLock, "context", id="soft-context"),
        pytest.param(SoftFileLock, "collect", id="soft-collect"),
    ],
)
def test_child_cleanup_preserves_parent_lock(
    tmp_path: Path,
    lock_type: type[BaseFileLock],
    action: Literal["release", "context", "collect"],
) -> None:
    path = str(tmp_path / "parent.lock")
    lock = lock_type(path, thread_local=False, is_singleton=False)
    proxy = lock.acquire()

    def cleanup_child() -> NoReturn:
        cleanup_child_hold(lock, proxy, action)

    _, status = os.waitpid(fork_process(cleanup_child), 0)

    assert os.waitstatus_to_exitcode(status) == 0
    assert not probe_lock(lock_type, path)
    lock.release()
    assert probe_lock(lock_type, path)


@NEEDS_FORK  # pragma: needs fork
@FORK_WARNING
@pytest.mark.parametrize(
    ("async_lock_type", "sync_lock_type"),
    [
        pytest.param(AsyncFileLock, FileLock, id="native"),
        pytest.param(AsyncSoftFileLock, SoftFileLock, id="soft"),
    ],
)
def test_async_child_release_preserves_parent_lock(
    tmp_path: Path,
    async_lock_type: type[BaseAsyncFileLock],
    sync_lock_type: type[BaseFileLock],
) -> None:
    path = str(tmp_path / "parent.lock")
    lock = async_lock_type(path, thread_local=False, is_singleton=False)
    asyncio.run(lock.acquire())

    def release_child() -> NoReturn:
        state = lock.is_locked, lock.lock_counter
        asyncio.run(lock.release(force=True))
        exit_child(0 if state == (False, 0) else 1)

    _, status = os.waitpid(fork_process(release_child), 0)

    assert os.waitstatus_to_exitcode(status) == 0
    assert not probe_lock(sync_lock_type, path)
    asyncio.run(lock.release())
    assert probe_lock(sync_lock_type, path)


@pytest.mark.requires_hard_links
@NEEDS_FORK  # pragma: needs fork
@FORK_WARNING
def test_soft_read_write_resets_older_same_path_instance(tmp_path: Path) -> None:  # pragma: needs hard-link
    path = str(tmp_path / "parent.lock")
    older = SoftReadWriteLock(path, is_singleton=False, heartbeat_interval=0.1, stale_threshold=0.5)
    newer = SoftReadWriteLock(path, is_singleton=False, heartbeat_interval=0.1, stale_threshold=0.5)
    del newer
    gc.collect()
    older.acquire_write()

    def inherited_child() -> NoReturn:
        with pytest.raises(RuntimeError, match="invalidated by fork"):
            older.acquire_read(timeout=0)
        older.release()
        exit_child(0)

    _, status = os.waitpid(fork_process(inherited_child), 0)

    assert os.waitstatus_to_exitcode(status) == 0
    contender = SoftReadWriteLock(path, is_singleton=False, heartbeat_interval=0.1, stale_threshold=0.5)
    with pytest.raises(Timeout):
        contender.acquire_write(timeout=0)
    contender.close()
    older.release()
    older.close()


@NEEDS_FORK  # pragma: needs fork
@FORK_WARNING
@pytest.mark.parametrize("lock_type", [pytest.param(FileLock, id="native"), pytest.param(SoftFileLock, id="soft")])
def test_child_singleton_registry_drops_parent_instance(tmp_path: Path, lock_type: type[BaseFileLock]) -> None:
    path = str(tmp_path / "parent.lock")
    parent_lock = lock_type(path, is_singleton=True)

    def singleton_child() -> NoReturn:
        exit_child(0 if lock_type(path, is_singleton=True) is not parent_lock else 1)

    _, status = os.waitpid(fork_process(singleton_child), 0)

    assert os.waitstatus_to_exitcode(status) == 0


@NEEDS_FORK  # pragma: needs fork
@FORK_WARNING
@pytest.mark.parametrize("lock_type", [pytest.param(FileLock, id="native"), pytest.param(SoftFileLock, id="soft")])
def test_inherited_idle_lock_rejects_acquire(tmp_path: Path, lock_type: type[BaseFileLock]) -> None:
    lock = lock_type(str(tmp_path / "parent.lock"), is_singleton=False)

    def inherited_child() -> NoReturn:
        try:
            lock.acquire(timeout=0)
        except RuntimeError as exception:
            exit_child(0 if "inherited across fork" in str(exception) else 1)
        exit_child(1)

    _, status = os.waitpid(fork_process(inherited_child), 0)

    assert os.waitstatus_to_exitcode(status) == 0


@NEEDS_FORK  # pragma: needs fork
@FORK_WARNING
@pytest.mark.parametrize(
    "lock_type", [pytest.param(AsyncFileLock, id="native"), pytest.param(AsyncSoftFileLock, id="soft")]
)
def test_inherited_idle_async_lock_rejects_acquire(tmp_path: Path, lock_type: type[BaseAsyncFileLock]) -> None:
    lock = lock_type(str(tmp_path / "parent.lock"), is_singleton=False)

    def inherited_child() -> NoReturn:
        try:
            asyncio.run(lock.acquire(timeout=0))
        except RuntimeError as exception:
            exit_child(0 if "inherited across fork" in str(exception) else 1)
        exit_child(1)

    _, status = os.waitpid(fork_process(inherited_child), 0)

    assert os.waitstatus_to_exitcode(status) == 0


@NEEDS_FORK  # pragma: needs fork
@FORK_WARNING
def test_fork_from_on_acquired_invalidates_child_acquire(tmp_path: Path) -> None:
    path = str(tmp_path / "parent.lock")
    fork_result = -1
    lock: FileLock

    def inherited_child() -> NoReturn:
        try:
            lock.acquire(timeout=0)
        except RuntimeError as exception:
            exit_child(0 if "inherited across fork" in str(exception) else 1)
        exit_child(1)

    def fork_from_hook(_fd: int) -> None:
        nonlocal fork_result
        fork_result = fork_process(inherited_child)

    lock = FileLock(path, thread_local=False, is_singleton=False, on_acquired=fork_from_hook)
    lock.acquire()
    _, status = os.waitpid(fork_result, 0)
    assert (os.waitstatus_to_exitcode(status), probe_lock(FileLock, path)) == (0, False)
    lock.release()


@NEEDS_FORK  # pragma: needs fork
@FORK_WARNING
def test_child_callback_registered_before_filelock_can_acquire(tmp_path: Path) -> None:
    script = """
import os
import sys

parent_path, child_path = sys.argv[1:]
callback_succeeded = False

def acquire_in_early_child_callback() -> None:
    global callback_succeeded
    with FileLock(child_path, is_singleton=False):
        pass
    callback_succeeded = True

os.register_at_fork(after_in_child=acquire_in_early_child_callback)

from filelock import FileLock, Timeout

parent = FileLock(parent_path, is_singleton=False)
parent.acquire()
child_pid = os.fork()
if child_pid == 0:
    os._exit(0 if callback_succeeded else 3)
_, status = os.waitpid(child_pid, 0)
if os.waitstatus_to_exitcode(status) != 0:
    sys.exit(1)

contender = FileLock(parent_path, is_singleton=False)
try:
    contender.acquire(timeout=0)
except Timeout:
    pass
else:
    sys.exit(2)
parent.release()
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
@pytest.mark.parametrize(
    "kind",
    [
        pytest.param("native", id="native"),
        pytest.param("soft", id="soft"),
        pytest.param("soft-rw", id="soft-read-write"),
    ],
)
@pytest.mark.requires_hard_links
def test_child_interpreter_exit_preserves_parent_lock(
    tmp_path: Path, kind: Literal["native", "soft", "soft-rw"]
) -> None:  # pragma: needs hard-link
    script = """
from __future__ import annotations

import os
import sys

from filelock import BaseFileLock, FileLock, SoftFileLock, SoftReadWriteLock, Timeout

def acquire(lock: BaseFileLock | SoftReadWriteLock, timeout: float = -1) -> None:
    if isinstance(lock, SoftReadWriteLock):
        lock.acquire_write(timeout=timeout)
    else:
        lock.acquire(timeout=timeout)

def release(lock: BaseFileLock | SoftReadWriteLock) -> None:
    if isinstance(lock, SoftReadWriteLock):
        lock.release()
        lock.close()
    else:
        lock.release()

kind, path = sys.argv[1:]
lock: BaseFileLock | SoftReadWriteLock
if kind == "soft-rw":
    lock = SoftReadWriteLock(path, is_singleton=False, heartbeat_interval=0.1, stale_threshold=30)
elif kind == "soft":
    lock = SoftFileLock(path, is_singleton=False)
else:
    lock = FileLock(path, is_singleton=False)
acquire(lock)

child_pid = os.fork()
if child_pid == 0:
    sys.exit(0)
_, status = os.waitpid(child_pid, 0)
if os.waitstatus_to_exitcode(status) != 0:
    sys.exit(1)

contender_pid = os.fork()
if contender_pid == 0:
    contender: BaseFileLock | SoftReadWriteLock
    if kind == "soft-rw":
        contender = SoftReadWriteLock(path, is_singleton=False, heartbeat_interval=0.1, stale_threshold=30)
    elif kind == "soft":
        contender = SoftFileLock(path, is_singleton=False)
    else:
        contender = FileLock(path, is_singleton=False)
    try:
        acquire(contender, timeout=0)
    except Timeout:
        os._exit(0)
    release(contender)
    os._exit(2)
_, status = os.waitpid(contender_pid, 0)
release(lock)
sys.exit(os.waitstatus_to_exitcode(status))
"""
    result = subprocess.run(
        [sys.executable, "-c", script, kind, str(tmp_path / "parent.lock")],
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )

    assert result.returncode == 0, result.stderr


@NEEDS_FORK  # pragma: needs fork
@FORK_WARNING
def test_fork_child_exits_when_another_threads_transition_never_drains(
    tmp_path: Path, mocker: MockerFixture
) -> None:  # pragma: win32 no cover
    mocker.patch("filelock._core._fork._FORK_DRAIN_TIMEOUT", 0.1)
    entered: Final[threading.Event] = threading.Event()
    never: Final[threading.Event] = threading.Event()

    class StuckReleaseLock(BaseFileLock):
        @override
        def _acquire(self) -> None:
            self._context.lock_file_fd = os.open(self.lock_file, os.O_CREAT | os.O_RDWR, 0o600)

        @override
        def _release(self) -> None:
            entered.set()
            never.wait()
            os.close(cast("int", self._context.lock_file_fd))
            self._context.lock_file_fd = None

    lock = StuckReleaseLock(str(tmp_path / "stuck.lock"), is_singleton=False, thread_local=False)
    lock.acquire()
    releaser = threading.Thread(target=lock.release, daemon=True)
    releaser.start()
    try:
        assert entered.wait(timeout=5)
        assert sys.platform != "win32"  # narrows os.fork for the type checker
        child_pid = os.fork()
        if child_pid == 0:  # pragma: forked child  # simplint: ignore[conditional-in-test]  # exits before cleanup
            os._exit(0)
        _, status = os.waitpid(child_pid, 0)
    finally:
        never.set()
        releaser.join(timeout=5)

    assert os.waitstatus_to_exitcode(status) == 70
