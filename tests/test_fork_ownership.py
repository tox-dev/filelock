from __future__ import annotations

import asyncio
import gc
import os
import subprocess  # ruff:ignore[suspicious-subprocess-import]  # fresh interpreter must register the callback before importing filelock
import sys
import threading
from errno import EBADF
from queue import Queue
from stat import S_ISDIR
from typing import TYPE_CHECKING, Final, Literal, NoReturn

import pytest
from fork_helpers import exit_child, fork_process

from filelock import (
    AcquireReturnProxy,
    AsyncFileLock,
    AsyncSoftFileLock,
    BaseAsyncFileLock,
    BaseFileLock,
    FileLock,
    SoftFileLock,
    SoftReadWriteLock,
    Timeout,
)

if sys.version_info >= (3, 11):
    from builtins import BaseExceptionGroup  # pragma: >=3.11 cover
else:  # pragma: <3.11 cover
    from exceptiongroup import BaseExceptionGroup

if TYPE_CHECKING:
    from pathlib import Path

    from pytest_mock import MockerFixture

_REQUIRES_FORK: Final[pytest.MarkDecorator] = pytest.mark.skipif(
    not (hasattr(os, "fork") and hasattr(os, "register_at_fork")), reason="os.fork and os.register_at_fork required"
)
_FORK_WARNING: Final[pytest.MarkDecorator] = pytest.mark.filterwarnings(
    "ignore:.*multi-threaded, use of fork.*:DeprecationWarning"
)


@_REQUIRES_FORK  # pragma: needs fork
@_FORK_WARNING
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
        _run_child_cleanup(lock, proxy, action)

    child_pid = fork_process(cleanup_child)
    _, status = os.waitpid(child_pid, 0)

    assert os.waitstatus_to_exitcode(status) == 0
    assert not _probe_lock(lock_type, path)
    lock.release()
    assert _probe_lock(lock_type, path)


@_REQUIRES_FORK  # pragma: needs fork
@_FORK_WARNING
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

    child_pid = fork_process(release_child)
    _, status = os.waitpid(child_pid, 0)

    assert os.waitstatus_to_exitcode(status) == 0
    assert not _probe_lock(sync_lock_type, path)
    asyncio.run(lock.release())
    assert _probe_lock(sync_lock_type, path)


@_REQUIRES_FORK  # pragma: needs fork
@_FORK_WARNING
def test_soft_read_write_resets_older_same_path_instance(tmp_path: Path) -> None:
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

    child_pid = fork_process(inherited_child)
    _, status = os.waitpid(child_pid, 0)

    assert os.waitstatus_to_exitcode(status) == 0
    contender = SoftReadWriteLock(path, is_singleton=False, heartbeat_interval=0.1, stale_threshold=0.5)
    with pytest.raises(Timeout):
        contender.acquire_write(timeout=0)
    contender.close()
    older.release()
    older.close()


@_REQUIRES_FORK  # pragma: needs fork
@_FORK_WARNING
def test_child_closes_descriptor_held_by_vanished_thread(tmp_path: Path) -> None:
    path = str(tmp_path / "parent.lock")
    descriptor: Queue[int] = Queue()
    acquired, release = threading.Event(), threading.Event()

    def hold_lock() -> None:
        lock = FileLock(path, thread_local=True, is_singleton=False, on_acquired=descriptor.put)
        with lock:
            acquired.set()
            release.wait()

    worker = threading.Thread(target=hold_lock)
    worker.start()
    assert acquired.wait(timeout=5)
    fd = descriptor.get(timeout=5)

    def descriptor_child() -> NoReturn:
        _assert_descriptor_closed(fd)

    child_pid = fork_process(descriptor_child)
    _, status = os.waitpid(child_pid, 0)
    release.set()
    worker.join(timeout=5)

    assert (os.waitstatus_to_exitcode(status), worker.is_alive()) == (0, False)


@_REQUIRES_FORK  # pragma: needs fork
@_FORK_WARNING
def test_fork_waits_for_descriptor_registration(tmp_path: Path, mocker: MockerFixture) -> None:
    path = str(tmp_path / "parent.lock")
    lock = FileLock(path, thread_local=False, is_singleton=False)
    entered, proceed, acquired = threading.Event(), threading.Event(), threading.Event()
    descriptors: list[int] = []
    real_fstat = os.fstat

    def delayed_fstat(fd: int) -> os.stat_result:
        if threading.current_thread() is worker and not entered.is_set():
            descriptors.append(fd)
            entered.set()
            proceed.wait()
        return real_fstat(fd)

    mocker.patch("os.fstat", side_effect=delayed_fstat)

    def acquire_lock() -> None:
        lock.acquire()
        acquired.set()

    worker = threading.Thread(target=acquire_lock)
    worker.start()
    assert entered.wait(timeout=5)
    timer = threading.Timer(0.05, proceed.set)
    timer.start()

    def descriptor_child() -> NoReturn:
        _assert_descriptor_closed(descriptors[0])

    child_pid = fork_process(descriptor_child)
    _, status = os.waitpid(child_pid, 0)
    timer.join(timeout=5)
    worker.join(timeout=5)

    assert (os.waitstatus_to_exitcode(status), acquired.is_set(), worker.is_alive()) == (0, True, False)
    lock.release()


@pytest.mark.skipif(not hasattr(os, "register_at_fork"), reason="fork transition gate requires register_at_fork")
def test_unrelated_acquisitions_reach_filesystem_boundary_concurrently(  # pragma: needs fork
    tmp_path: Path, mocker: MockerFixture
) -> None:
    boundary = threading.Barrier(3, timeout=5)
    real_fstat = os.fstat

    def wait_at_boundary(fd: int) -> os.stat_result:
        # Only the two named workers stat while the patch is installed, so the guard never takes its false arc.
        if threading.current_thread().name.startswith("concurrent-acquire-"):  # pragma: no branch
            boundary.wait()
        return real_fstat(fd)

    mocker.patch("os.fstat", side_effect=wait_at_boundary)

    def acquire_lock(path: str) -> None:
        with FileLock(path, is_singleton=False):
            pass

    workers = [
        threading.Thread(
            target=acquire_lock,
            args=(str(tmp_path / f"{index}.lock"),),
            name=f"concurrent-acquire-{index}",
        )
        for index in range(2)
    ]
    for worker in workers:
        worker.start()
    try:
        boundary.wait()
    finally:
        for worker in workers:
            worker.join(timeout=5)

    assert [worker.is_alive() for worker in workers] == [False, False]


@_REQUIRES_FORK  # pragma: needs fork
@_FORK_WARNING
@pytest.mark.parametrize("lock_type", [pytest.param(FileLock, id="native"), pytest.param(SoftFileLock, id="soft")])
def test_child_singleton_registry_drops_parent_instance(tmp_path: Path, lock_type: type[BaseFileLock]) -> None:
    path = str(tmp_path / "parent.lock")
    parent_lock = lock_type(path, is_singleton=True)

    def singleton_child() -> NoReturn:
        child_lock = lock_type(path, is_singleton=True)
        exit_child(0 if child_lock is not parent_lock else 1)

    child_pid = fork_process(singleton_child)
    _, status = os.waitpid(child_pid, 0)

    assert os.waitstatus_to_exitcode(status) == 0


@_REQUIRES_FORK  # pragma: needs fork
@_FORK_WARNING
@pytest.mark.parametrize("lock_type", [pytest.param(FileLock, id="native"), pytest.param(SoftFileLock, id="soft")])
def test_inherited_idle_lock_rejects_acquire(tmp_path: Path, lock_type: type[BaseFileLock]) -> None:
    lock = lock_type(str(tmp_path / "parent.lock"), is_singleton=False)

    def inherited_child() -> NoReturn:
        try:
            lock.acquire(timeout=0)
        except RuntimeError as exception:
            exit_child(0 if "inherited across fork" in str(exception) else 1)
        exit_child(1)

    child_pid = fork_process(inherited_child)
    _, status = os.waitpid(child_pid, 0)

    assert os.waitstatus_to_exitcode(status) == 0


@_REQUIRES_FORK  # pragma: needs fork
@_FORK_WARNING
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

    child_pid = fork_process(inherited_child)
    _, status = os.waitpid(child_pid, 0)

    assert os.waitstatus_to_exitcode(status) == 0


@_REQUIRES_FORK  # pragma: needs fork
@_FORK_WARNING
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
    assert (os.waitstatus_to_exitcode(status), _probe_lock(FileLock, path)) == (0, False)
    lock.release()


@pytest.mark.skipif(not hasattr(os, "register_at_fork"), reason="descriptor registry requires register_at_fork")
def test_reader_directory_registration_failure_closes_descriptor(  # pragma: needs fork
    tmp_path: Path, mocker: MockerFixture
) -> None:
    lock = SoftReadWriteLock(
        str(tmp_path / "parent.lock"),
        is_singleton=False,
        heartbeat_interval=0.1,
        stale_threshold=0.5,
    )
    real_fstat = os.fstat
    directory_fds: list[int] = []

    def fail_directory_fstat(fd: int) -> os.stat_result:
        stat_result = real_fstat(fd)
        assert S_ISDIR(stat_result.st_mode)
        directory_fds.append(fd)
        msg = "directory identity unavailable"
        raise OSError(msg)

    mocker.patch("os.fstat", side_effect=fail_directory_fstat)
    with pytest.raises(OSError, match="directory identity unavailable"):
        lock.acquire_read()

    with pytest.raises(OSError, match=rf"\[Errno {EBADF}\]"):
        real_fstat(directory_fds[0])
    lock.close()


@pytest.mark.skipif(not hasattr(os, "register_at_fork"), reason="descriptor registry requires register_at_fork")
def test_reader_directory_registration_and_close_errors_are_grouped(  # pragma: needs fork
    tmp_path: Path, mocker: MockerFixture
) -> None:
    lock = SoftReadWriteLock(
        str(tmp_path / "parent.lock"),
        is_singleton=False,
        heartbeat_interval=0.1,
        stale_threshold=0.5,
    )
    real_close, real_fstat = os.close, os.fstat
    directory_fds: list[int] = []

    def fail_directory_fstat(fd: int) -> os.stat_result:
        stat_result = real_fstat(fd)
        assert S_ISDIR(stat_result.st_mode)
        directory_fds.append(fd)
        msg = "directory identity unavailable"
        raise OSError(msg)

    def fail_directory_close(fd: int) -> None:
        assert fd == directory_fds[0]
        msg = "directory close failed"
        raise OSError(msg)

    mocker.patch("os.fstat", side_effect=fail_directory_fstat)
    mocker.patch("os.close", side_effect=fail_directory_close)
    with pytest.raises(BaseExceptionGroup) as info:
        lock.acquire_read()
    real_close(directory_fds[0])
    lock.close()

    assert [(type(error), str(error)) for error in info.value.exceptions] == [
        (OSError, "directory identity unavailable"),
        (OSError, "directory close failed"),
    ]


@_REQUIRES_FORK  # pragma: needs fork
@_FORK_WARNING
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


@_REQUIRES_FORK  # pragma: needs fork
@_FORK_WARNING
@pytest.mark.parametrize(
    "kind",
    [
        pytest.param("native", id="native"),
        pytest.param("soft", id="soft"),
        pytest.param("soft-rw", id="soft-read-write"),
    ],
)
def test_child_interpreter_exit_preserves_parent_lock(
    tmp_path: Path, kind: Literal["native", "soft", "soft-rw"]
) -> None:
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


def _run_child_cleanup(
    lock: BaseFileLock,
    proxy: AcquireReturnProxy,
    action: Literal["release", "context", "collect"],
) -> NoReturn:
    assert (lock.is_locked, lock.lock_counter) == (False, 0)
    if action == "release":
        lock.release(force=True)
    elif action == "context":
        proxy.__exit__(None, None, None)
    else:
        del proxy
        del lock
        gc.collect()
    exit_child(0)


def _probe_lock(lock_type: type[BaseFileLock], path: str) -> bool:  # pragma: needs fork
    read_fd, write_fd = os.pipe()

    def probe_child() -> NoReturn:
        os.close(read_fd)
        candidate = lock_type(path, is_singleton=False)
        try:
            candidate.acquire(timeout=0)
        except Timeout:
            acquired = False
        else:
            acquired = True
            candidate.release()
        os.write(write_fd, b"1" if acquired else b"0")
        os.close(write_fd)
        exit_child(0)

    child_pid = fork_process(probe_child)
    os.close(write_fd)
    result = os.read(read_fd, 1)
    os.close(read_fd)
    _, status = os.waitpid(child_pid, 0)
    assert os.waitstatus_to_exitcode(status) == 0
    return result == b"1"


def _assert_descriptor_closed(fd: int) -> NoReturn:
    try:
        os.fstat(fd)
    except OSError as exception:
        exit_child(0 if exception.errno == EBADF else 1)
    exit_child(1)
