from __future__ import annotations

import os
import subprocess  # ruff:ignore[suspicious-subprocess-import]  # isolated interpreter controls child callback registration order
import sys
import threading
from errno import EBADF
from typing import TYPE_CHECKING, cast

import pytest

from filelock import BaseFileLock
from filelock._core import override
from tests.capability_marks import NEEDS_FORK

if TYPE_CHECKING:
    from pathlib import Path

    from pytest_mock import MockerFixture

from tests.core.fork_probes import error_details, fork_descriptor_probe


@NEEDS_FORK  # pragma: needs fork
def test_third_party_descriptor_is_closed_in_child(tmp_path: Path) -> None:
    descriptors: list[int] = []
    lock = _DescriptorLock(str(tmp_path / "third-party.lock"), is_singleton=False, on_acquired=descriptors.append)
    lock.acquire()

    _, status = os.waitpid(fork_descriptor_probe(descriptors[0], os.fstat), 0)
    lock.release()

    assert os.waitstatus_to_exitcode(status) == 0


class _DescriptorLock(BaseFileLock):
    acquire_error: BaseException | None = None
    descriptor: int | None = None
    fail_release: bool = False

    @override  # pragma: needs fork
    def _acquire(self) -> None:  # pragma: needs fork
        self.descriptor = self._context.lock_file_fd = os.open(self.lock_file, os.O_CREAT | os.O_RDWR, 0o600)
        if self.acquire_error is not None:
            raise self.acquire_error

    @override  # pragma: needs fork
    def _release(self) -> None:  # pragma: needs fork
        if self.fail_release:
            msg = "unlock failed"
            raise RuntimeError(msg)
        os.close(cast("int", self._context.lock_file_fd))
        self._context.lock_file_fd = None


def test_direct_descriptor_backend_keeps_new_hold_on_interrupted_release(tmp_path: Path, mocker: MockerFixture) -> None:
    lock_path = tmp_path / "third-party.lock"
    lock = _DescriptorLock(str(lock_path), thread_local=False, is_singleton=False)
    lock.acquire()
    start, entered, finish = (threading.Event() for _ in range(3))

    def hold_replacement() -> None:
        start.wait()
        with lock:
            entered.set()
            finish.wait()

    worker = threading.Thread(target=hold_replacement, daemon=True)
    worker.start()
    release_current = lock._release_current
    pending = [None]

    def release_then_interrupt(*, force: bool) -> None:
        release_current(force=force)
        if pending:
            pending.pop()
            start.set()
            assert not entered.wait(0.1)
            raise KeyboardInterrupt

    mocker.patch.object(lock, "_release_current", side_effect=release_then_interrupt, autospec=True)
    try:
        with pytest.raises(KeyboardInterrupt):
            lock.release()
        assert entered.wait(1)
        fd = lock._context.lock_file_fd
        assert fd is not None
        assert os.fstat(fd).st_ino == lock_path.stat().st_ino
    finally:
        finish.set()
        worker.join(2)
        lock.release(force=True)


@NEEDS_FORK  # pragma: needs fork
def test_unlock_failure_keeps_descriptor_registered(tmp_path: Path) -> None:
    descriptors: list[int] = []
    lock = _DescriptorLock(str(tmp_path / "retry.lock"), is_singleton=False, on_acquired=descriptors.append)
    lock.acquire()
    lock.fail_release = True
    with pytest.raises(RuntimeError, match="unlock failed"):
        lock.release()

    _, status = os.waitpid(fork_descriptor_probe(descriptors[0], os.fstat), 0)
    lock.fail_release = False
    lock.release()

    assert os.waitstatus_to_exitcode(status) == 0


@NEEDS_FORK  # pragma: needs fork
def test_acquisition_failure_keeps_descriptor_registered_until_rollback(tmp_path: Path) -> None:
    lock = _DescriptorLock(str(tmp_path / "partial.lock"), is_singleton=False)
    lock.acquire_error = RuntimeError("acquire failed")
    lock.fail_release = True
    with pytest.raises(BaseExceptionGroup) as info:
        lock.acquire(timeout=0)
    _, status = os.waitpid(fork_descriptor_probe(cast("int", lock.descriptor), os.fstat), 0)
    lock.acquire_error = None
    lock.fail_release = False
    lock.release()

    assert (
        error_details(info.value),
        os.waitstatus_to_exitcode(status),
    ) == (
        [(RuntimeError, "acquire failed"), (RuntimeError, "unlock failed")],
        0,
    )


@pytest.mark.parametrize(
    ("fail_release", "expected"),
    [
        pytest.param(
            False,
            [(RuntimeError, "acquire failed"), (OSError, "registration failed")],
            id="rollback-succeeds",
        ),
        pytest.param(
            True,
            [
                (RuntimeError, "acquire failed"),
                (OSError, "registration failed"),
                (RuntimeError, "unlock failed"),
            ],
            id="rollback-fails",
        ),
    ],
)
@NEEDS_FORK  # pragma: needs fork
def test_acquisition_and_registration_errors_are_grouped(
    tmp_path: Path,
    mocker: MockerFixture,
    *,
    fail_release: bool,
    expected: list[tuple[type[BaseException], str]],
) -> None:
    lock = _DescriptorLock(str(tmp_path / "registration.lock"), is_singleton=False)
    lock.acquire_error = RuntimeError("acquire failed")
    lock.fail_release = fail_release
    real_fstat = os.fstat

    def fail_registration(fd: int) -> os.stat_result:
        assert fd == lock.descriptor
        msg = "registration failed"
        raise OSError(msg)

    fstat_mock = mocker.patch("os.fstat", side_effect=fail_registration, autospec=True)
    with pytest.raises(BaseExceptionGroup) as info:
        lock.acquire(timeout=0)
    descriptor = cast("int", lock.descriptor)
    mocker.stop(fstat_mock)

    _, status = os.waitpid(fork_descriptor_probe(descriptor, real_fstat), 0)
    lock.acquire_error = None
    lock.fail_release = False
    lock.release(force=True)

    with pytest.raises(OSError, match=rf"\[Errno {EBADF}\]"):
        real_fstat(descriptor)
    assert (error_details(info.value), os.waitstatus_to_exitcode(status)) == (expected, 0)


@NEEDS_FORK  # pragma: needs fork
def test_registration_failure_tracks_descriptor_when_rollback_fails(tmp_path: Path, mocker: MockerFixture) -> None:
    lock = _DescriptorLock(str(tmp_path / "group.lock"), is_singleton=False)
    lock.fail_release = True
    real_fstat = os.fstat

    def fail_registration(fd: int) -> os.stat_result:
        assert fd == lock.descriptor
        msg = "registration failed"
        raise OSError(msg)

    fstat_mock = mocker.patch("os.fstat", side_effect=fail_registration, autospec=True)
    with pytest.raises(BaseExceptionGroup) as info:
        lock.acquire(timeout=0)
    descriptor = cast("int", lock.descriptor)
    mocker.stop(fstat_mock)

    _, status = os.waitpid(fork_descriptor_probe(descriptor, real_fstat), 0)
    lock.fail_release = False
    lock.release()

    with pytest.raises(OSError, match=rf"\[Errno {EBADF}\]"):
        real_fstat(descriptor)
    assert (error_details(info.value), os.waitstatus_to_exitcode(status)) == (
        [(OSError, "registration failed"), (RuntimeError, "unlock failed")],
        0,
    )


@NEEDS_FORK  # pragma: needs fork
def test_unverified_descriptor_does_not_close_reused_child_fd(tmp_path: Path) -> None:
    script = """
from __future__ import annotations

import os
import sys


descriptor = -1
replacement_descriptor = -1
real_fstat = os.fstat

def replace_descriptor_in_child() -> None:
    global replacement_descriptor
    os.close(descriptor)
    replacement_descriptor = os.open(sys.argv[2], os.O_CREAT | os.O_RDWR, 0o600)

os.register_at_fork(after_in_child=replace_descriptor_in_child)

from filelock import BaseFileLock

class FailingRollbackLock(BaseFileLock):
    fail_release = True

    def _acquire(self) -> None:
        global descriptor
        descriptor = self._context.lock_file_fd = os.open(self.lock_file, os.O_CREAT | os.O_RDWR, 0o600)

    def _release(self) -> None:
        if self.fail_release:
            raise RuntimeError("rollback failed")
        os.close(self._context.lock_file_fd if self._context.lock_file_fd is not None else -1)
        self._context.lock_file_fd = None

def fail_descriptor_probe(fd: int) -> os.stat_result:
    if fd == descriptor:
        raise OSError("registration failed")
    return real_fstat(fd)

os.fstat = fail_descriptor_probe
lock = FailingRollbackLock(sys.argv[1], is_singleton=False)
try:
    lock.acquire(timeout=0)
except BaseExceptionGroup:
    pass
else:
    raise SystemExit(2)

child_pid = os.fork()
if child_pid == 0:
    try:
        real_fstat(replacement_descriptor)
    except OSError:
        os._exit(1)
    os._exit(0 if replacement_descriptor == descriptor else 2)

_, status = os.waitpid(child_pid, 0)
os.fstat = real_fstat
lock.fail_release = False
lock.release()
raise SystemExit(os.waitstatus_to_exitcode(status))
"""
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            script,
            str(tmp_path / "lock"),
            str(tmp_path / "replacement"),
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert (result.returncode, result.stderr) == (0, "")


@NEEDS_FORK  # pragma: needs fork
def test_control_flow_registration_error_rolls_back_descriptor(tmp_path: Path, mocker: MockerFixture) -> None:
    descriptors: list[int] = []

    class StopAcquire(BaseException):
        pass

    class CapturingLock(_DescriptorLock):
        @override
        def _acquire(self) -> None:
            super()._acquire()
            descriptors.append(cast("int", self._context.lock_file_fd))

    real_fstat = os.fstat

    def interrupt_registration(fd: int) -> os.stat_result:
        if fd == descriptors[0]:
            raise StopAcquire
        return real_fstat(fd)

    mocker.patch("os.fstat", side_effect=interrupt_registration, autospec=True)
    with pytest.raises(StopAcquire):
        CapturingLock(str(tmp_path / "interrupt.lock"), is_singleton=False).acquire(timeout=0)

    with pytest.raises(OSError, match=rf"\[Errno {EBADF}\]"):
        real_fstat(descriptors[0])
