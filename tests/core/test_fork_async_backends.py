from __future__ import annotations

import asyncio
import os
from errno import EBADF
from typing import TYPE_CHECKING, cast

import pytest

from filelock import BaseAsyncFileLock
from filelock._core import override
from tests.capability_marks import NEEDS_FORK, NEEDS_GENERATOR_EXCEPTION_CONTEXT

if TYPE_CHECKING:
    from pathlib import Path

    from pytest_mock import MockerFixture

from tests.core.fork_probes import error_details, fork_descriptor_probe


@pytest.mark.asyncio
@NEEDS_FORK  # pragma: needs fork
async def test_coroutine_registration_error_rolls_back_descriptor(tmp_path: Path, mocker: MockerFixture) -> None:
    lock = _CoroutineDescriptorLock(str(tmp_path / "coroutine.lock"), is_singleton=False)
    real_fstat = os.fstat

    def fail_registration(fd: int) -> os.stat_result:
        if fd == lock.descriptor:
            msg = "registration failed"
            raise OSError(msg)
        return real_fstat(fd)

    mocker.patch("os.fstat", side_effect=fail_registration, autospec=True)
    with pytest.raises(OSError, match="registration failed"):
        await lock.acquire(timeout=0)

    with pytest.raises(OSError, match=rf"\[Errno {EBADF}\]"):
        real_fstat(cast("int", lock.descriptor))


class _CoroutineDescriptorLock(BaseAsyncFileLock):
    acquire_error: BaseException | None = None
    acquire_error_before_descriptor: BaseException | None = None
    descriptor: int | None = None
    release_error: BaseException | None = None
    release_error_before_close: BaseException | None = None

    @override
    async def _acquire(self) -> None:  # ty: ignore[incompatible-method-override]  # coroutine backends are supported
        if self.acquire_error_before_descriptor is not None:
            raise self.acquire_error_before_descriptor
        self.descriptor = self._context.lock_file_fd = os.open(self.lock_file, os.O_CREAT | os.O_RDWR, 0o600)
        if self.acquire_error is not None:  # pragma: needs fork
            raise self.acquire_error

    @override
    async def _release(self) -> None:  # ty: ignore[incompatible-method-override]  # coroutine backends are supported
        if self.release_error_before_close is not None:  # pragma: needs fork
            raise self.release_error_before_close
        os.close(cast("int", self._context.lock_file_fd))
        self._context.lock_file_fd = None
        if self.release_error is not None:
            raise self.release_error


@pytest.mark.asyncio
async def test_coroutine_acquisition_failure_before_descriptor_propagates(tmp_path: Path) -> None:
    acquisition_error = RuntimeError("acquire failed")
    lock = _CoroutineDescriptorLock(str(tmp_path / "coroutine-empty.lock"), is_singleton=False)
    lock.acquire_error_before_descriptor = acquisition_error

    with pytest.raises(RuntimeError, match="acquire failed") as info:
        await lock.acquire(timeout=0)

    assert (info.value is acquisition_error, lock.is_locked) == (True, False)


@NEEDS_FORK  # pragma: needs fork
@pytest.mark.asyncio
async def test_coroutine_acquisition_failure_keeps_descriptor_registered_until_rollback(tmp_path: Path) -> None:
    lock = _CoroutineDescriptorLock(
        str(tmp_path / "coroutine-partial.lock"), is_singleton=False, context_error_policy="group"
    )
    lock.acquire_error = RuntimeError("acquire failed")
    lock.release_error = RuntimeError("release failed")
    with pytest.raises(BaseExceptionGroup) as info:
        await lock.acquire(timeout=0)

    _, status = await asyncio.to_thread(os.waitpid, fork_descriptor_probe(cast("int", lock.descriptor), os.fstat), 0)
    lock.acquire_error = None
    lock.release_error = None
    await lock.release()

    assert (error_details(info.value), os.waitstatus_to_exitcode(status)) == (
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
@NEEDS_FORK  # pragma: needs fork
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

    fstat_mock = mocker.patch("os.fstat", side_effect=fail_registration, autospec=True)
    with pytest.raises(BaseExceptionGroup) as info:
        await lock.acquire(timeout=0)
    descriptor = cast("int", lock.descriptor)
    mocker.stop(fstat_mock)

    # A fork can reuse the closed descriptor number.
    with pytest.raises(OSError, match=rf"\[Errno {EBADF}\]"):
        real_fstat(descriptor)

    _, status = await asyncio.to_thread(os.waitpid, fork_descriptor_probe(descriptor, real_fstat), 0)
    lock.acquire_error = None
    lock.release_error = None
    await lock.release(force=True)

    assert (error_details(info.value), os.waitstatus_to_exitcode(status)) == (expected, 0)


@NEEDS_FORK  # pragma: needs fork
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

    fstat_mock = mocker.patch("os.fstat", side_effect=fail_registration, autospec=True)
    with pytest.raises(BaseExceptionGroup) as info:
        await lock.acquire(timeout=0)
    descriptor = cast("int", lock.descriptor)
    mocker.stop(fstat_mock)

    _, status = await asyncio.to_thread(os.waitpid, fork_descriptor_probe(descriptor, real_fstat), 0)
    lock.release_error_before_close = None
    await lock.release()

    with pytest.raises(OSError, match=rf"\[Errno {EBADF}\]"):
        real_fstat(descriptor)
    assert (error_details(info.value), os.waitstatus_to_exitcode(status)) == (
        [(OSError, "registration failed"), (RuntimeError, "rollback failed")],
        0,
    )


@NEEDS_GENERATOR_EXCEPTION_CONTEXT
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

    assert error_details(info.value) == [
        (RuntimeError, "hook failed"),
        (RuntimeError, "release failed"),
    ]
