from __future__ import annotations

import asyncio
import gc
import os
import subprocess  # ruff:ignore[suspicious-subprocess-import]  # fresh interpreter must register the callback before importing filelock
import sys
import threading
from errno import EBADF
from queue import Queue
from typing import TYPE_CHECKING, Final, Literal, NoReturn, cast

import pytest

from filelock import (
    AcquireReturnProxy,
    AsyncFileLock,
    AsyncSoftFileLock,
    AsyncSoftReadWriteLock,
    BaseAsyncFileLock,
    BaseFileLock,
    FileLock,
    SoftFileLock,
    SoftReadWriteLock,
    Timeout,
)
from tests.capability_marks import NEEDS_FORK, NEEDS_REGISTER_AT_FORK
from tests.fork_helpers import exit_child, fork_process

if TYPE_CHECKING:
    from pathlib import Path

    from pytest_mock import MockerFixture

_FORK_WARNING: Final[pytest.MarkDecorator] = pytest.mark.filterwarnings(
    "ignore:.*multi-threaded, use of fork.*:DeprecationWarning"
)


@NEEDS_FORK  # pragma: needs fork
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

    _, status = os.waitpid(fork_process(cleanup_child), 0)

    assert os.waitstatus_to_exitcode(status) == 0
    assert not _probe_lock(lock_type, path)
    lock.release()
    assert _probe_lock(lock_type, path)


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


@NEEDS_FORK  # pragma: needs fork
@_FORK_WARNING
@pytest.mark.parametrize("lock_type", [pytest.param(FileLock, id="native"), pytest.param(SoftFileLock, id="soft")])
def test_child_with_matching_numeric_pid_preserves_parent_lock(
    tmp_path: Path, mocker: MockerFixture, lock_type: type[BaseFileLock]
) -> None:
    path = str(tmp_path / "parent.lock")
    lock = lock_type(path, is_singleton=False)
    proxy = lock.acquire()
    pid_mock = mocker.patch("filelock._api.os.getpid", return_value=os.getpid(), autospec=True)

    def cleanup_child() -> NoReturn:
        with pytest.raises(RuntimeError, match="inherited across fork"):
            lock.acquire(timeout=0)
        _run_child_cleanup(lock, proxy, "context")

    try:
        try:
            _, status = os.waitpid(fork_process(cleanup_child), 0)
        finally:
            mocker.stop(pid_mock)
        assert os.waitstatus_to_exitcode(status) == 0
        assert not _probe_lock(lock_type, path)
    finally:
        lock.release()
    assert _probe_lock(lock_type, path)


@NEEDS_FORK  # pragma: needs fork
@_FORK_WARNING
@pytest.mark.skipif(sys.platform != "linux", reason="requires Linux PID namespaces")
@pytest.mark.parametrize("action", ["release", "acquire", "inherited_acquire"])
def test_early_child_callback_with_matching_pid(tmp_path: Path, action: str) -> None:  # pragma: linux cover
    script = """
import os
import sys
from unittest.mock import patch

action, parent_path, child_path = sys.argv[1:]
child_lock = None
callback_error = None

def early_child_callback():
    global child_lock, callback_error
    if action == "release":
        parent.release()
    elif action == "inherited_acquire":
        try:
            parent.acquire(timeout=0)
        except RuntimeError as error:
            callback_error = str(error)
    else:
        child_lock = FileLock(child_path, is_singleton=False)
        child_lock.acquire()

os.register_at_fork(after_in_child=early_child_callback)

from filelock import FileLock, Timeout, _api

parent = FileLock(parent_path, is_singleton=False)
parent.acquire()
real_pid = os.getpid
parent_pid = real_pid()
parent_namespace = _api._FORK_STATE.process_identity[1]

def namespace():
    return parent_namespace if real_pid() == parent_pid else (-1, -1)

with patch("filelock._api._pid_namespace", side_effect=namespace), patch(
    "filelock._api.os.getpid", return_value=parent_pid
):
    child_pid = os.fork()
    if child_pid == 0:
        if action == "inherited_acquire" and (callback_error is None or "inherited across fork" not in callback_error):
            os._exit(5)
        if (parent.is_locked, parent.lock_counter) != (False, 0):
            os._exit(1)
        if action == "acquire" and (child_lock.is_locked, child_lock.lock_counter) != (True, 1):
            os._exit(2)
        os._exit(0)
    _, status = os.waitpid(child_pid, 0)

if os.waitstatus_to_exitcode(status) != 0:
    sys.exit(3)
try:
    FileLock(parent_path, is_singleton=False).acquire(timeout=0)
except Timeout:
    pass
else:
    sys.exit(4)
parent.release()
"""
    result = subprocess.run(
        [sys.executable, "-c", script, action, str(tmp_path / "parent.lock"), str(tmp_path / "child.lock")],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert (result.returncode, result.stderr) == (0, "")


@pytest.mark.parametrize(
    ("thread_offset", "namespace"),
    [pytest.param(0, None, id="forking-thread"), pytest.param(1, (0, 0), id="other-thread")],
)
def test_parent_keeps_hold_during_fork(
    tmp_path: Path, mocker: MockerFixture, thread_offset: int, namespace: tuple[int, int] | None
) -> None:
    from filelock import _api  # simplint: ignore[import-outside-top-level]  # simulates the at-fork boundary

    lock = SoftFileLock(tmp_path / "parent.lock", is_singleton=False)
    with lock:
        mocker.patch.dict(_api._FORK_STATE.fork_owner_depths, {threading.get_ident() + thread_offset: 1})
        mocker.patch("filelock._api._pid_namespace", return_value=namespace, autospec=True)
        with lock.acquire(timeout=0):
            assert lock.lock_counter == 2


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        pytest.param(FileNotFoundError(), None, id="unavailable"),
        pytest.param(os.stat_result((0, 22, 11, 0, 0, 0, 0, 0, 0, 0)), (11, 22), id="available"),
    ],
)
def test_pid_namespace_identity(
    mocker: MockerFixture, result: os.stat_result | OSError, expected: tuple[int, int] | None
) -> None:
    from filelock import _api  # simplint: ignore[import-outside-top-level]  # tests the optional /proc fallback

    mocker.patch("filelock._api.sys.platform", "linux")
    mocker.patch("filelock._api.pathlib.Path.stat", side_effect=[result], autospec=True)

    assert _api._pid_namespace() == expected


@pytest.mark.requires_hard_links
@NEEDS_FORK  # pragma: needs fork
@_FORK_WARNING
def test_soft_read_write_child_with_matching_pid(
    tmp_path: Path, mocker: MockerFixture
) -> None:  # pragma: needs hard-link
    path = str(tmp_path / "parent.lock")
    lock = SoftReadWriteLock(path, is_singleton=False, heartbeat_interval=0.1, stale_threshold=0.5)
    lock.acquire_write()
    pid_mock = mocker.patch("filelock._api.os.getpid", return_value=os.getpid(), autospec=True)

    def inherited_child() -> NoReturn:
        with pytest.raises(RuntimeError, match="invalidated by fork"):
            lock.acquire_write(timeout=0)
        lock.release()
        lock.close()
        exit_child(0)

    try:
        try:
            _, status = os.waitpid(fork_process(inherited_child), 0)
        finally:
            mocker.stop(pid_mock)
        assert os.waitstatus_to_exitcode(status) == 0
        contender = SoftReadWriteLock(path, is_singleton=False, heartbeat_interval=0.1, stale_threshold=0.5)
        with pytest.raises(Timeout):
            contender.acquire_write(timeout=0)
        contender.close()
    finally:
        lock.release()
        lock.close()


@pytest.mark.requires_hard_links
@NEEDS_FORK  # pragma: needs fork
@_FORK_WARNING
@pytest.mark.skipif(sys.platform != "linux", reason="requires Linux PID namespaces")
@pytest.mark.parametrize("action", ["release", "close", "acquire"])
def test_early_soft_read_write_child_callback_with_matching_pid(  # pragma: linux cover  # pragma: needs hard-link
    tmp_path: Path, action: str
) -> None:
    script = """
import os
import sys
from unittest.mock import patch

action, path = sys.argv[1:]
callback_error = None

def early_child_callback():
    global callback_error
    if action == "acquire":
        try:
            parent.acquire_write(timeout=0)
        except RuntimeError as error:
            callback_error = str(error)
    else:
        getattr(parent, action)()

os.register_at_fork(after_in_child=early_child_callback)

from filelock import SoftReadWriteLock, Timeout, _api

parent = SoftReadWriteLock(path, is_singleton=False, heartbeat_interval=0.1, stale_threshold=0.5)
parent.acquire_write()
real_pid = os.getpid
parent_pid = real_pid()
parent_namespace = _api._FORK_STATE.process_identity[1]

def namespace():
    return parent_namespace if real_pid() == parent_pid else (-1, -1)

with patch("filelock._api._pid_namespace", side_effect=namespace), patch(
    "filelock._api.os.getpid", return_value=parent_pid
):
    child_pid = os.fork()
    if child_pid == 0:
        if action == "acquire" and (callback_error is None or "invalidated by fork" not in callback_error):
            os._exit(5)
        try:
            parent.acquire_write(timeout=0)
        except RuntimeError as error:
            os._exit(0 if "invalidated by fork" in str(error) else 1)
        os._exit(2)
    _, status = os.waitpid(child_pid, 0)

if os.waitstatus_to_exitcode(status) != 0:
    sys.exit(3)
contender = SoftReadWriteLock(path, is_singleton=False, heartbeat_interval=0.1, stale_threshold=0.5)
try:
    contender.acquire_write(timeout=0)
except Timeout:
    pass
else:
    sys.exit(4)
contender.close()
parent.release()
parent.close()
"""
    result = subprocess.run(
        [sys.executable, "-W", "ignore::DeprecationWarning", "-c", script, action, str(tmp_path / "parent.lock")],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert (result.returncode, result.stderr) == (0, "")


@pytest.mark.requires_hard_links
@NEEDS_FORK  # pragma: needs fork
@_FORK_WARNING
def test_async_soft_read_write_child_with_matching_pid(
    tmp_path: Path, mocker: MockerFixture
) -> None:  # pragma: needs hard-link
    lock = AsyncSoftReadWriteLock(str(tmp_path / "parent.lock"), is_singleton=False)
    pid_mock = mocker.patch("filelock._api.os.getpid", return_value=os.getpid(), autospec=True)

    def inherited_child() -> NoReturn:
        with pytest.raises(RuntimeError, match="inherited across fork"):
            asyncio.run(lock.acquire_write(timeout=0))
        asyncio.run(lock.release())
        asyncio.run(lock.close())
        exit_child(0)

    try:
        _, status = os.waitpid(fork_process(inherited_child), 0)
    finally:
        mocker.stop(pid_mock)
    assert os.waitstatus_to_exitcode(status) == 0
    asyncio.run(lock.close())


@NEEDS_FORK  # pragma: needs fork
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

    _, status = os.waitpid(fork_process(release_child), 0)

    assert os.waitstatus_to_exitcode(status) == 0
    assert not _probe_lock(sync_lock_type, path)
    asyncio.run(lock.release())
    assert _probe_lock(sync_lock_type, path)


@pytest.mark.requires_hard_links
@NEEDS_FORK  # pragma: needs fork
@_FORK_WARNING
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
@_FORK_WARNING
def test_child_closes_descriptor_held_by_vanished_thread(tmp_path: Path) -> None:
    path = str(tmp_path / "parent.lock")
    descriptor: Queue[int] = Queue()
    acquired, release = threading.Event(), threading.Event()

    def hold_lock() -> None:
        with FileLock(path, thread_local=True, is_singleton=False, on_acquired=descriptor.put):
            acquired.set()
            release.wait()

    worker = threading.Thread(target=hold_lock)
    worker.start()
    assert acquired.wait(timeout=5)
    fd = descriptor.get(timeout=5)

    def descriptor_child() -> NoReturn:
        _assert_descriptor_closed(fd)

    _, status = os.waitpid(fork_process(descriptor_child), 0)
    release.set()
    worker.join(timeout=5)

    assert (os.waitstatus_to_exitcode(status), worker.is_alive()) == (0, False)


def _assert_descriptor_closed(fd: int) -> NoReturn:
    try:
        os.fstat(fd)
    except OSError as exception:
        exit_child(0 if exception.errno == EBADF else 1)
    exit_child(1)


@NEEDS_FORK  # pragma: needs fork
@_FORK_WARNING
def test_fork_waits_for_descriptor_registration(tmp_path: Path, mocker: MockerFixture) -> None:
    lock = FileLock(str(tmp_path / "parent.lock"), thread_local=False, is_singleton=False)
    entered, proceed, acquired = threading.Event(), threading.Event(), threading.Event()
    descriptors: list[int] = []
    real_fstat = os.fstat

    def delayed_fstat(fd: int) -> os.stat_result:
        if threading.current_thread() is worker and not entered.is_set():
            descriptors.append(fd)
            entered.set()
            proceed.wait()
        return real_fstat(fd)

    mocker.patch("os.fstat", side_effect=delayed_fstat, autospec=True)

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

    _, status = os.waitpid(fork_process(descriptor_child), 0)
    timer.join(timeout=5)
    worker.join(timeout=5)

    assert (os.waitstatus_to_exitcode(status), acquired.is_set(), worker.is_alive()) == (0, True, False)
    lock.release()


@NEEDS_REGISTER_AT_FORK
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

    mocker.patch("os.fstat", side_effect=wait_at_boundary, autospec=True)

    def acquire_lock(path: str) -> None:
        with FileLock(path, mode=0o600, is_singleton=False):
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


@NEEDS_FORK  # pragma: needs fork
@_FORK_WARNING
@pytest.mark.parametrize("lock_type", [pytest.param(FileLock, id="native"), pytest.param(SoftFileLock, id="soft")])
def test_child_singleton_registry_drops_parent_instance(tmp_path: Path, lock_type: type[BaseFileLock]) -> None:
    path = str(tmp_path / "parent.lock")
    parent_lock = lock_type(path, is_singleton=True)

    def singleton_child() -> NoReturn:
        exit_child(0 if lock_type(path, is_singleton=True) is not parent_lock else 1)

    _, status = os.waitpid(fork_process(singleton_child), 0)

    assert os.waitstatus_to_exitcode(status) == 0


@NEEDS_FORK  # pragma: needs fork
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

    _, status = os.waitpid(fork_process(inherited_child), 0)

    assert os.waitstatus_to_exitcode(status) == 0


@NEEDS_FORK  # pragma: needs fork
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

    _, status = os.waitpid(fork_process(inherited_child), 0)

    assert os.waitstatus_to_exitcode(status) == 0


@NEEDS_FORK  # pragma: needs fork
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


@NEEDS_FORK  # pragma: needs fork
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


@NEEDS_FORK  # pragma: needs fork
@_FORK_WARNING
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
@_FORK_WARNING
def test_fork_child_exits_when_another_threads_transition_never_drains(
    tmp_path: Path, mocker: MockerFixture
) -> None:  # pragma: win32 no cover
    mocker.patch("filelock._api._FORK_DRAIN_TIMEOUT", 0.1)
    entered: Final[threading.Event] = threading.Event()
    never: Final[threading.Event] = threading.Event()

    class StuckReleaseLock(BaseFileLock):
        def _acquire(self) -> None:
            self._context.lock_file_fd = os.open(self.lock_file, os.O_CREAT | os.O_RDWR, 0o600)

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
