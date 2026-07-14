from __future__ import annotations

import asyncio
import os
import subprocess  # noqa: S404  # isolated interpreter controls child callback registration order
import sys
from errno import EBADF
from typing import TYPE_CHECKING, Final, NoReturn, cast

import pytest
from fork_helpers import exit_child, fork_process

from filelock import BaseAsyncFileLock, BaseFileLock

if sys.version_info >= (3, 11):
    from builtins import BaseExceptionGroup
else:  # pragma: no cover (<py311)
    from exceptiongroup import BaseExceptionGroup

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from pytest_mock import MockerFixture

_REQUIRES_FORK: Final[pytest.MarkDecorator] = pytest.mark.skipif(not hasattr(os, "fork"), reason="os.fork required")


class _DescriptorLock(BaseFileLock):
    acquire_error: BaseException | None = None
    descriptor: int | None = None
    fail_release: bool = False

    def _acquire(self) -> None:
        self.descriptor = self._context.lock_file_fd = os.open(self.lock_file, os.O_CREAT | os.O_RDWR, 0o600)
        if self.acquire_error is not None:
            raise self.acquire_error

    def _release(self) -> None:
        if self.fail_release:
            msg = "unlock failed"
            raise RuntimeError(msg)
        os.close(cast("int", self._context.lock_file_fd))
        self._context.lock_file_fd = None


class _CoroutineDescriptorLock(BaseAsyncFileLock):
    acquire_error: BaseException | None = None
    acquire_error_before_descriptor: BaseException | None = None
    descriptor: int | None = None
    release_error: BaseException | None = None
    release_error_before_close: BaseException | None = None

    async def _acquire(self) -> None:  # ty: ignore[invalid-method-override]  # coroutine backends are supported
        if self.acquire_error_before_descriptor is not None:
            raise self.acquire_error_before_descriptor
        self.descriptor = self._context.lock_file_fd = os.open(self.lock_file, os.O_CREAT | os.O_RDWR, 0o600)
        if self.acquire_error is not None:
            raise self.acquire_error

    async def _release(self) -> None:  # ty: ignore[invalid-method-override]  # coroutine backends are supported
        if self.release_error_before_close is not None:
            raise self.release_error_before_close
        os.close(cast("int", self._context.lock_file_fd))
        self._context.lock_file_fd = None
        if self.release_error is not None:
            raise self.release_error


@_REQUIRES_FORK
def test_third_party_descriptor_is_closed_in_child(tmp_path: Path) -> None:
    descriptors: list[int] = []
    lock = _DescriptorLock(str(tmp_path / "third-party.lock"), is_singleton=False, on_acquired=descriptors.append)
    lock.acquire()

    child_pid = _fork_descriptor_probe(descriptors[0], os.fstat)
    _, status = os.waitpid(child_pid, 0)
    lock.release()

    assert os.waitstatus_to_exitcode(status) == 0


@_REQUIRES_FORK
def test_unlock_failure_keeps_descriptor_registered(tmp_path: Path) -> None:
    descriptors: list[int] = []
    lock = _DescriptorLock(str(tmp_path / "retry.lock"), is_singleton=False, on_acquired=descriptors.append)
    lock.acquire()
    lock.fail_release = True
    with pytest.raises(RuntimeError, match="unlock failed"):
        lock.release()

    child_pid = _fork_descriptor_probe(descriptors[0], os.fstat)
    _, status = os.waitpid(child_pid, 0)
    lock.fail_release = False
    lock.release()

    assert os.waitstatus_to_exitcode(status) == 0


@_REQUIRES_FORK
def test_acquisition_failure_keeps_descriptor_registered_until_rollback(tmp_path: Path) -> None:
    lock = _DescriptorLock(str(tmp_path / "partial.lock"), is_singleton=False)
    lock.acquire_error = RuntimeError("acquire failed")
    lock.fail_release = True
    with pytest.raises(BaseExceptionGroup) as info:
        lock.acquire(timeout=0)
    child_pid = _fork_descriptor_probe(cast("int", lock.descriptor), os.fstat)
    _, status = os.waitpid(child_pid, 0)
    lock.acquire_error = None
    lock.fail_release = False
    lock.release()

    assert (
        _error_details(info.value),
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
@_REQUIRES_FORK
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

    fstat_mock = mocker.patch("os.fstat", side_effect=fail_registration)
    with pytest.raises(BaseExceptionGroup) as info:
        lock.acquire(timeout=0)
    descriptor = cast("int", lock.descriptor)
    mocker.stop(fstat_mock)

    child_pid = _fork_descriptor_probe(descriptor, real_fstat)
    _, status = os.waitpid(child_pid, 0)
    lock.acquire_error = None
    lock.fail_release = False
    lock.release(force=True)

    with pytest.raises(OSError, match=rf"\[Errno {EBADF}\]"):
        real_fstat(descriptor)
    assert (_error_details(info.value), os.waitstatus_to_exitcode(status)) == (expected, 0)


@_REQUIRES_FORK
def test_registration_failure_tracks_descriptor_when_rollback_fails(tmp_path: Path, mocker: MockerFixture) -> None:
    lock = _DescriptorLock(str(tmp_path / "group.lock"), is_singleton=False)
    lock.fail_release = True
    real_fstat = os.fstat

    def fail_registration(fd: int) -> os.stat_result:
        assert fd == lock.descriptor
        msg = "registration failed"
        raise OSError(msg)

    fstat_mock = mocker.patch("os.fstat", side_effect=fail_registration)
    with pytest.raises(BaseExceptionGroup) as info:
        lock.acquire(timeout=0)
    descriptor = cast("int", lock.descriptor)
    mocker.stop(fstat_mock)

    child_pid = _fork_descriptor_probe(descriptor, real_fstat)
    _, status = os.waitpid(child_pid, 0)
    lock.fail_release = False
    lock.release()

    with pytest.raises(OSError, match=rf"\[Errno {EBADF}\]"):
        real_fstat(descriptor)
    assert (_error_details(info.value), os.waitstatus_to_exitcode(status)) == (
        [(OSError, "registration failed"), (RuntimeError, "unlock failed")],
        0,
    )


@_REQUIRES_FORK
def test_unverified_descriptor_does_not_close_reused_child_fd(tmp_path: Path) -> None:
    script = """
from __future__ import annotations

import os
import sys

if sys.version_info >= (3, 11):
    from builtins import BaseExceptionGroup
else:
    from exceptiongroup import BaseExceptionGroup

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


@_REQUIRES_FORK
def test_control_flow_registration_error_rolls_back_descriptor(tmp_path: Path, mocker: MockerFixture) -> None:
    descriptors: list[int] = []

    class StopAcquire(BaseException):
        pass

    class CapturingLock(_DescriptorLock):
        def _acquire(self) -> None:
            super()._acquire()
            descriptors.append(cast("int", self._context.lock_file_fd))

    real_fstat = os.fstat

    def interrupt_registration(fd: int) -> os.stat_result:
        assert fd == descriptors[0]
        raise StopAcquire

    mocker.patch("os.fstat", side_effect=interrupt_registration)
    with pytest.raises(StopAcquire):
        CapturingLock(str(tmp_path / "interrupt.lock"), is_singleton=False).acquire(timeout=0)

    with pytest.raises(OSError, match=rf"\[Errno {EBADF}\]"):
        real_fstat(descriptors[0])


@pytest.mark.asyncio
@_REQUIRES_FORK
async def test_coroutine_registration_error_rolls_back_descriptor(tmp_path: Path, mocker: MockerFixture) -> None:
    descriptors: list[int] = []
    real_fstat = os.fstat

    def fail_registration(fd: int) -> os.stat_result:
        descriptors.append(fd)
        msg = "registration failed"
        raise OSError(msg)

    mocker.patch("os.fstat", side_effect=fail_registration)
    with pytest.raises(OSError, match="registration failed"):
        await _CoroutineDescriptorLock(str(tmp_path / "coroutine.lock"), is_singleton=False).acquire(timeout=0)

    with pytest.raises(OSError, match=rf"\[Errno {EBADF}\]"):
        real_fstat(descriptors[0])


@pytest.mark.asyncio
async def test_coroutine_acquisition_failure_before_descriptor_propagates(tmp_path: Path) -> None:
    acquisition_error = RuntimeError("acquire failed")
    lock = _CoroutineDescriptorLock(str(tmp_path / "coroutine-empty.lock"), is_singleton=False)
    lock.acquire_error_before_descriptor = acquisition_error

    with pytest.raises(RuntimeError, match="acquire failed") as info:
        await lock.acquire(timeout=0)

    assert (info.value is acquisition_error, lock.is_locked) == (True, False)


@_REQUIRES_FORK
@pytest.mark.asyncio
async def test_coroutine_acquisition_failure_keeps_descriptor_registered_until_rollback(tmp_path: Path) -> None:
    lock = _CoroutineDescriptorLock(
        str(tmp_path / "coroutine-partial.lock"), is_singleton=False, context_error_policy="group"
    )
    lock.acquire_error = RuntimeError("acquire failed")
    lock.release_error = RuntimeError("release failed")
    with pytest.raises(BaseExceptionGroup) as info:
        await lock.acquire(timeout=0)
    descriptor = cast("int", lock.descriptor)

    child_pid = _fork_descriptor_probe(descriptor, os.fstat)
    _, status = await asyncio.to_thread(os.waitpid, child_pid, 0)
    lock.acquire_error = None
    lock.release_error = None
    await lock.release()

    assert (_error_details(info.value), os.waitstatus_to_exitcode(status)) == (
        [(RuntimeError, "acquire failed"), (RuntimeError, "release failed")],
        0,
    )


@pytest.mark.parametrize(
    ("release_error", "expected"),
    [
        pytest.param(
            None,
            [(RuntimeError, "acquire failed"), (OSError, "registration failed")],
            id="rollback-succeeds",
        ),
        pytest.param(
            RuntimeError("release failed"),
            [
                (RuntimeError, "acquire failed"),
                (OSError, "registration failed"),
                (RuntimeError, "release failed"),
            ],
            id="rollback-fails",
        ),
    ],
)
@_REQUIRES_FORK
@pytest.mark.asyncio
async def test_coroutine_acquisition_and_registration_errors_are_grouped(
    tmp_path: Path,
    mocker: MockerFixture,
    release_error: BaseException | None,
    expected: list[tuple[type[BaseException], str]],
) -> None:
    lock = _CoroutineDescriptorLock(str(tmp_path / "coroutine-registration.lock"), is_singleton=False)
    lock.acquire_error = RuntimeError("acquire failed")
    lock.release_error = release_error
    real_fstat = os.fstat

    def fail_registration(fd: int) -> os.stat_result:
        assert fd == lock.descriptor
        msg = "registration failed"
        raise OSError(msg)

    fstat_mock = mocker.patch("os.fstat", side_effect=fail_registration)
    with pytest.raises(BaseExceptionGroup) as info:
        await lock.acquire(timeout=0)
    descriptor = cast("int", lock.descriptor)
    mocker.stop(fstat_mock)

    child_pid = _fork_descriptor_probe(descriptor, real_fstat)
    _, status = await asyncio.to_thread(os.waitpid, child_pid, 0)
    lock.acquire_error = None
    lock.release_error = None
    await lock.release(force=True)

    with pytest.raises(OSError, match=rf"\[Errno {EBADF}\]"):
        real_fstat(descriptor)
    assert (_error_details(info.value), os.waitstatus_to_exitcode(status)) == (expected, 0)


@_REQUIRES_FORK
@pytest.mark.asyncio
async def test_coroutine_registration_failure_tracks_descriptor_when_rollback_fails(
    tmp_path: Path, mocker: MockerFixture
) -> None:
    lock = _CoroutineDescriptorLock(str(tmp_path / "coroutine-group.lock"), is_singleton=False)
    lock.release_error_before_close = RuntimeError("rollback failed")
    real_fstat = os.fstat

    def fail_registration(fd: int) -> os.stat_result:
        assert fd == lock.descriptor
        msg = "registration failed"
        raise OSError(msg)

    fstat_mock = mocker.patch("os.fstat", side_effect=fail_registration)
    with pytest.raises(BaseExceptionGroup) as info:
        await lock.acquire(timeout=0)
    descriptor = cast("int", lock.descriptor)
    mocker.stop(fstat_mock)

    child_pid = _fork_descriptor_probe(descriptor, real_fstat)
    _, status = await asyncio.to_thread(os.waitpid, child_pid, 0)
    lock.release_error_before_close = None
    await lock.release()

    with pytest.raises(OSError, match=rf"\[Errno {EBADF}\]"):
        real_fstat(descriptor)
    assert (_error_details(info.value), os.waitstatus_to_exitcode(status)) == (
        [(OSError, "registration failed"), (RuntimeError, "rollback failed")],
        0,
    )


@pytest.mark.asyncio
async def test_coroutine_on_acquired_error_preserves_context(tmp_path: Path) -> None:
    callback_error = RuntimeError("hook failed")
    prior_context = LookupError("prior callback failure")
    callback_error.__context__ = prior_context

    def fail_callback(_fd: int) -> None:
        raise callback_error

    lock = _CoroutineDescriptorLock(
        str(tmp_path / "coroutine-hook-context.lock"), is_singleton=False, on_acquired=fail_callback
    )
    with pytest.raises(RuntimeError, match="hook failed") as info:
        await lock.acquire(timeout=0)

    assert (info.value is callback_error, callback_error.__context__, lock.is_locked) == (True, prior_context, False)


@pytest.mark.asyncio
async def test_coroutine_on_acquired_and_rollback_errors_are_grouped(tmp_path: Path) -> None:
    callback_error = RuntimeError("hook failed")

    def fail_callback(_fd: int) -> None:
        raise callback_error

    lock = _CoroutineDescriptorLock(
        str(tmp_path / "coroutine-hook.lock"), is_singleton=False, on_acquired=fail_callback
    )
    lock.release_error = RuntimeError("release failed")
    with pytest.raises(BaseExceptionGroup) as info:
        await lock.acquire(timeout=0)

    assert _error_details(info.value) == [
        (RuntimeError, "hook failed"),
        (RuntimeError, "release failed"),
    ]


def _error_details(group: BaseExceptionGroup) -> list[tuple[type[BaseException], str]]:
    return [(type(error), str(error)) for error in group.exceptions]


def _fork_descriptor_probe(descriptor: int, stat_descriptor: Callable[[int], os.stat_result]) -> int:
    def probe_child() -> NoReturn:
        with pytest.raises(OSError, match=rf"\[Errno {EBADF}\]"):
            stat_descriptor(descriptor)
        exit_child(0)

    return fork_process(probe_child)
