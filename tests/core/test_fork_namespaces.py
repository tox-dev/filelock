from __future__ import annotations

import asyncio
import os
import subprocess  # ruff:ignore[suspicious-subprocess-import]  # fresh interpreter must register the callback before importing filelock
import sys
import threading
from typing import TYPE_CHECKING, NoReturn

import pytest

from filelock import (
    AsyncSoftReadWriteLock,
    BaseFileLock,
    FileLock,
    SoftFileLock,
    SoftReadWriteLock,
    Timeout,
)
from filelock._core._fork import _FORK_STATE, _pid_namespace
from tests.capability_marks import NEEDS_FORK
from tests.fork_helpers import exit_child, fork_process

if TYPE_CHECKING:
    from pathlib import Path

    from pytest_mock import MockerFixture

from tests.core.fork_probes import FORK_WARNING, cleanup_child_hold, probe_lock


@NEEDS_FORK  # pragma: needs fork
@FORK_WARNING
@pytest.mark.parametrize("lock_type", [pytest.param(FileLock, id="native"), pytest.param(SoftFileLock, id="soft")])
def test_child_with_matching_numeric_pid_preserves_parent_lock(
    tmp_path: Path, mocker: MockerFixture, lock_type: type[BaseFileLock]
) -> None:
    path = str(tmp_path / "parent.lock")
    lock = lock_type(path, is_singleton=False)
    proxy = lock.acquire()
    pid_mock = mocker.patch("filelock._core._fork.os.getpid", return_value=os.getpid(), autospec=True)

    def cleanup_child() -> NoReturn:
        with pytest.raises(RuntimeError, match="inherited across fork"):
            lock.acquire(timeout=0)
        cleanup_child_hold(lock, proxy, "context")

    try:
        try:
            _, status = os.waitpid(fork_process(cleanup_child), 0)
        finally:
            mocker.stop(pid_mock)
        assert os.waitstatus_to_exitcode(status) == 0
        assert not probe_lock(lock_type, path)
    finally:
        lock.release()
    assert probe_lock(lock_type, path)


@NEEDS_FORK  # pragma: needs fork
@FORK_WARNING
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

from filelock import FileLock, Timeout
from filelock._core._fork import _FORK_STATE

parent = FileLock(parent_path, is_singleton=False)
parent.acquire()
real_pid = os.getpid
parent_pid = real_pid()
parent_namespace = _FORK_STATE.process_identity[1]

def namespace():
    return parent_namespace if real_pid() == parent_pid else (-1, -1)

with patch("filelock._core._fork._pid_namespace", side_effect=namespace), patch(
    "filelock._core._fork.os.getpid", return_value=parent_pid
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
    lock = SoftFileLock(tmp_path / "parent.lock", is_singleton=False)
    with lock:
        mocker.patch.dict(_FORK_STATE.fork_owner_depths, {threading.get_ident() + thread_offset: 1})
        mocker.patch("filelock._core._fork._pid_namespace", return_value=namespace, autospec=True)
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
    mocker.patch("filelock._core._fork.sys.platform", "linux")
    mocker.patch("filelock._core._fork.pathlib.Path.stat", side_effect=[result], autospec=True)

    assert _pid_namespace() == expected


@pytest.mark.requires_hard_links
@NEEDS_FORK  # pragma: needs fork
@FORK_WARNING
def test_soft_read_write_child_with_matching_pid(
    tmp_path: Path, mocker: MockerFixture
) -> None:  # pragma: needs hard-link
    path = str(tmp_path / "parent.lock")
    lock = SoftReadWriteLock(path, is_singleton=False, heartbeat_interval=0.1, stale_threshold=0.5)
    lock.acquire_write()
    pid_mock = mocker.patch("filelock._core._fork.os.getpid", return_value=os.getpid(), autospec=True)

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
@FORK_WARNING
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

from filelock import SoftReadWriteLock, Timeout
from filelock._core._fork import _FORK_STATE

parent = SoftReadWriteLock(path, is_singleton=False, heartbeat_interval=0.1, stale_threshold=0.5)
parent.acquire_write()
real_pid = os.getpid
parent_pid = real_pid()
parent_namespace = _FORK_STATE.process_identity[1]

def namespace():
    return parent_namespace if real_pid() == parent_pid else (-1, -1)

with patch("filelock._core._fork._pid_namespace", side_effect=namespace), patch(
    "filelock._core._fork.os.getpid", return_value=parent_pid
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
@FORK_WARNING
def test_async_soft_read_write_child_with_matching_pid(
    tmp_path: Path, mocker: MockerFixture
) -> None:  # pragma: needs hard-link
    lock = AsyncSoftReadWriteLock(str(tmp_path / "parent.lock"), is_singleton=False)
    pid_mock = mocker.patch("filelock._core._fork.os.getpid", return_value=os.getpid(), autospec=True)

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
