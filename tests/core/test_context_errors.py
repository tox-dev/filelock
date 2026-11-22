from __future__ import annotations

import os
import traceback
from errno import EINTR, EIO, ENOSPC
from typing import TYPE_CHECKING

import pytest

from filelock import (
    BaseFileLock,
    ContextErrorPolicy,
    FileLock,
    SoftFileLock,
)
from filelock._core import append_exception_context, raise_grouped_errors
from tests.capability_marks import (
    NEEDS_FCNTL,
    WINDOWS_ONLY,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from contextlib import AbstractContextManager
    from pathlib import Path

    from pytest_mock import MockerFixture


@pytest.mark.parametrize(
    "enter",
    [
        pytest.param(lambda lock: lock, id="direct"),
        pytest.param(BaseFileLock.acquire, id="proxy"),
    ],
)
def test_context_group_detaches_release_context(
    tmp_path: Path,
    close_failure: tuple[Callable[[int], None], OSError, RuntimeError],
    *,
    enter: Callable[[BaseFileLock], AbstractContextManager[BaseFileLock]],
) -> None:
    capture, release_error, release_cause = close_failure
    body_error = ValueError("body failed")
    body_cause = LookupError("body cause")
    lock = FileLock(str(tmp_path / "a"), context_error_policy="group", close_error_policy="raise", on_acquired=capture)
    with pytest.raises(ExceptionGroup) as info, enter(lock):
        raise body_error from body_cause
    assert (
        info.value.exceptions,
        body_error.__context__,
        release_error.__context__,
        body_error.__cause__,
        release_error.__cause__,
        body_error.__traceback__ is not None,
        release_error.__traceback__ is not None,
    ) == ((body_error, release_error), None, None, body_cause, release_cause, True, True)


def test_context_group_handles_deep_body_context(
    tmp_path: Path,
    close_failure: tuple[Callable[[int], None], OSError, RuntimeError],
) -> None:
    capture, release_error, _ = close_failure
    body_error = ValueError("body failed")
    context: BaseException = body_error
    for index in range(2_000):
        next_context = RuntimeError(str(index))
        context.__context__ = next_context
        context = next_context
    lock = FileLock(str(tmp_path / "a"), context_error_policy="group", close_error_policy="raise", on_acquired=capture)

    with pytest.raises(ExceptionGroup) as info, lock:
        raise body_error

    assert (info.value.exceptions, release_error.__context__) == ((body_error, release_error), None)


def test_context_group_detaches_equivalent_shared_exception_dag(
    tmp_path: Path,
    close_failure: tuple[Callable[[int], None], OSError, RuntimeError],
) -> None:
    capture, release_error, _ = close_failure
    body_error: BaseException = ValueError("shared leaf")
    context_error = body_error
    for depth in range(25):
        body_error = ExceptionGroup(str(depth), (body_error, body_error))
        context_error = ExceptionGroup(str(depth), (context_error, context_error))
    lock = FileLock(str(tmp_path / "a"), context_error_policy="group", close_error_policy="raise", on_acquired=capture)

    try:
        raise context_error
    except ExceptionGroup:
        with pytest.raises(ExceptionGroup) as info, lock:
            raise body_error  # ruff:ignore[raise-without-from-inside-except]  # build the caller-controlled implicit context graph

    assert (info.value.exceptions, body_error.__context__, release_error.__context__) == (
        (body_error, release_error),
        None,
        None,
    )


def test_context_group_preserves_distinct_shared_exception_dag(
    tmp_path: Path,
    close_failure: tuple[Callable[[int], None], OSError, RuntimeError],
) -> None:
    capture, release_error, _ = close_failure
    body_error: BaseException = ValueError("body leaf")
    context_error: BaseException = TypeError("context leaf")
    for depth in range(25):
        body_error = ExceptionGroup(str(depth), (body_error, body_error))
        context_error = ExceptionGroup(str(depth), (context_error, context_error))
    lock = FileLock(str(tmp_path / "a"), context_error_policy="group", close_error_policy="raise", on_acquired=capture)

    try:
        raise context_error
    except ExceptionGroup:
        with pytest.raises(ExceptionGroup) as info, lock:
            raise body_error  # ruff:ignore[raise-without-from-inside-except]  # build the caller-controlled implicit context graph

    assert (info.value.exceptions, body_error.__context__, release_error.__context__) == (
        (body_error, release_error),
        context_error,
        None,
    )


@pytest.mark.parametrize(
    "enter",
    [
        pytest.param(lambda lock: lock, id="direct"),
        pytest.param(BaseFileLock.acquire, id="proxy"),
    ],
)
def test_context_group_renders_independent_leaves(
    tmp_path: Path,
    close_failure: tuple[Callable[[int], None], OSError, RuntimeError],
    *,
    enter: Callable[[BaseFileLock], AbstractContextManager[BaseFileLock]],
) -> None:
    capture, release_error, _ = close_failure
    release_error.__cause__ = None
    release_error.__suppress_context__ = False
    body_error = ValueError("body failed")
    lock = FileLock(str(tmp_path / "a"), context_error_policy="group", close_error_policy="raise", on_acquired=capture)
    with pytest.raises(ExceptionGroup) as info, enter(lock):
        raise body_error
    group_rendering = "".join(traceback.format_exception(info.value))
    release_rendering = "".join(traceback.format_exception(release_error))
    assert (
        sum(line.endswith("ValueError: body failed") for line in group_rendering.splitlines()),
        sum(line.endswith("ValueError: body failed") for line in release_rendering.splitlines()),
        sum(line.endswith("RuntimeError: release cause") for line in release_rendering.splitlines()),
        sum(line.endswith("OSError: release failed") for line in release_rendering.splitlines()),
    ) == (1, 0, 0, 1)


@pytest.mark.parametrize(
    "enter",
    [
        pytest.param(lambda lock: lock, id="direct"),
        pytest.param(BaseFileLock.acquire, id="proxy"),
    ],
)
def test_context_chain_keeps_release_error_with_body_in_context(
    tmp_path: Path,
    close_failure: tuple[Callable[[int], None], OSError, RuntimeError],
    *,
    enter: Callable[[BaseFileLock], AbstractContextManager[BaseFileLock]],
) -> None:
    capture, release_error, _ = close_failure
    body_error = ValueError("body failed")
    lock = FileLock(str(tmp_path / "a"), context_error_policy="chain", close_error_policy="raise", on_acquired=capture)
    with pytest.raises(OSError, match="release failed") as info, enter(lock):
        raise body_error
    assert (info.value, release_error.__context__) == (release_error, body_error)


@pytest.mark.parametrize(
    "policy",
    [pytest.param("chain", id="chain"), pytest.param("group", id="group")],
)
@pytest.mark.parametrize(
    "enter",
    [
        pytest.param(lambda lock: lock, id="direct"),
        pytest.param(BaseFileLock.acquire, id="proxy"),
    ],
)
def test_context_body_only_failure_propagates_body(
    tmp_path: Path, policy: ContextErrorPolicy, *, enter: Callable[[BaseFileLock], AbstractContextManager[BaseFileLock]]
) -> None:
    lock = SoftFileLock(str(tmp_path / "a"), context_error_policy=policy)
    body = ValueError("body failed")
    with pytest.raises(ValueError, match="body failed") as info, enter(lock):
        raise body
    assert info.value is body


@pytest.mark.parametrize(
    "policy",
    [pytest.param("chain", id="chain"), pytest.param("group", id="group")],
)
@pytest.mark.parametrize(
    "enter",
    [
        pytest.param(lambda lock: lock, id="direct"),
        pytest.param(BaseFileLock.acquire, id="proxy"),
    ],
)
def test_context_release_only_failure_propagates_release(
    tmp_path: Path,
    close_failure: tuple[Callable[[int], None], OSError, RuntimeError],
    policy: ContextErrorPolicy,
    *,
    enter: Callable[[BaseFileLock], AbstractContextManager[BaseFileLock]],
) -> None:
    capture, release_error, release_cause = close_failure
    lock = FileLock(str(tmp_path / "a"), context_error_policy=policy, close_error_policy="raise", on_acquired=capture)
    with pytest.raises(OSError, match="release failed") as info, enter(lock):
        pass
    assert (info.value, release_error.__context__, release_error.__cause__) == (release_error, None, release_cause)


def test_context_group_base_exception_leaf_is_base_group(
    tmp_path: Path, close_failure: tuple[Callable[[int], None], OSError, RuntimeError]
) -> None:
    capture, _, _ = close_failure
    lock = FileLock(str(tmp_path / "a"), context_error_policy="group", close_error_policy="raise", on_acquired=capture)
    with pytest.raises(BaseExceptionGroup) as info, lock:
        raise KeyboardInterrupt
    assert not isinstance(info.value, ExceptionGroup)  # a BaseException leaf stays outside except Exception
    assert [type(leaf) for leaf in info.value.exceptions] == [KeyboardInterrupt, OSError]


def test_invalid_context_error_policy_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="context_error_policy must be"):
        SoftFileLock(str(tmp_path / "a"), context_error_policy="explode")  # ty: ignore[invalid-argument-type]  # the invalid policy is the input under test


def test_singleton_rejects_different_context_policy(tmp_path: Path) -> None:
    path = str(tmp_path / "a")
    first = SoftFileLock(path, is_singleton=True, context_error_policy="chain")
    try:
        with pytest.raises(ValueError, match="context_error_policy"):
            SoftFileLock(path, is_singleton=True, context_error_policy="group")
    finally:
        first.release(force=True)


@NEEDS_FCNTL  # pragma: needs fcntl
def test_close_error_default_suppressed_on_unix(tmp_path: Path, mocker: MockerFixture) -> None:
    lock = FileLock(str(tmp_path / "a"))
    lock.acquire()
    _fail_close_of(mocker, lock, OSError(EIO, "close failed"))
    lock.release()  # Unix default drops a FUSE/Docker EIO
    assert not lock.is_locked


def _fail_close_of(mocker: MockerFixture, lock: BaseFileLock, error: OSError) -> list[int]:
    # Other locks can close during garbage collection on this thread.
    fd = lock._context.lock_file_fd
    real_close = os.close
    attempts: list[int] = []

    def close(target: int) -> None:
        if target == fd:
            attempts.append(target)
            raise error
        real_close(target)  # pragma: no cover  # only an unrelated __del__ close during the mock window reaches here

    mocker.patch("os.close", side_effect=close, autospec=True)
    return attempts


@WINDOWS_ONLY  # pragma: win32 cover
def test_close_error_default_raises_on_windows(tmp_path: Path, mocker: MockerFixture) -> None:
    lock = FileLock(str(tmp_path / "a"))
    lock.acquire()
    _fail_close_of(mocker, lock, OSError(EIO, "close failed"))
    with pytest.raises(OSError, match="close failed"):
        lock.release()
    assert not lock.is_locked


def test_close_error_suppress(tmp_path: Path, mocker: MockerFixture) -> None:
    lock = FileLock(str(tmp_path / "a"), close_error_policy="suppress")
    lock.acquire()
    _fail_close_of(mocker, lock, OSError(EIO, "close failed"))
    lock.release()
    assert not lock.is_locked


@pytest.mark.parametrize("errno", [EINTR, EIO, ENOSPC])
def test_close_error_raise_is_exact_and_committed(tmp_path: Path, mocker: MockerFixture, errno: int) -> None:
    lock = FileLock(str(tmp_path / "a"), close_error_policy="raise")
    lock.acquire()
    injected = OSError(errno, "close failed")
    attempts = _fail_close_of(mocker, lock, injected)
    with pytest.raises(OSError, match="close failed") as info:
        lock.release()
    assert info.value is injected
    assert len(attempts) == 1
    assert not lock.is_locked
    assert lock.lock_counter == 0


@NEEDS_FCNTL  # pragma: needs fcntl
def test_close_not_reached_when_unlock_fails(tmp_path: Path, mocker: MockerFixture) -> None:
    lock = FileLock(str(tmp_path / "a"), close_error_policy="raise")
    lock.acquire()
    fd = lock._context.lock_file_fd
    mocker.patch("filelock._native._unix.fcntl.flock", side_effect=[OSError(EIO, "unlock failed"), None], autospec=True)
    real_close = os.close
    closed: list[int] = []
    mocker.patch(
        "os.close",
        side_effect=lambda target: closed.append(target) or real_close(target),
        autospec=True,
    )
    with pytest.raises(OSError, match="unlock failed"):
        lock.release()
    assert lock.is_locked
    assert fd not in closed
    lock.release()
    assert not lock.is_locked
    assert fd in closed


def test_dual_body_and_close_failure_grouped(tmp_path: Path, mocker: MockerFixture) -> None:
    lock = FileLock(str(tmp_path / "a"), close_error_policy="raise", context_error_policy="group")
    lock.acquire()
    _fail_close_of(mocker, lock, OSError("close failed"))
    body = ValueError("body failed")
    with pytest.raises(ExceptionGroup) as info:
        lock._release_in_context(body)
    leaf_body, close = info.value.exceptions
    assert isinstance(leaf_body, ValueError)
    assert isinstance(close, OSError)


def test_invalid_close_error_policy_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="close_error_policy must be"):
        FileLock(str(tmp_path / "a"), close_error_policy="explode")  # ty: ignore[invalid-argument-type]  # the invalid policy is the input under test


def test_singleton_rejects_different_close_policy(tmp_path: Path) -> None:
    path = str(tmp_path / "a")
    first = FileLock(path, is_singleton=True, close_error_policy="raise")
    try:
        with pytest.raises(ValueError, match="close_error_policy"):
            FileLock(path, is_singleton=True, close_error_policy="suppress")
    finally:
        first.release(force=True)


def test_grouped_errors_keep_a_context_that_is_a_distinct_group() -> None:
    context = ExceptionGroup("context group", [ValueError("a")])
    member = ExceptionGroup("member group", [KeyError("b"), KeyError("c")])
    member.__context__ = context

    with pytest.raises(BaseExceptionGroup) as info:
        raise_grouped_errors("grouped", member, RuntimeError("other"))

    assert member.__context__ is context
    assert member in info.value.exceptions


def test_grouped_errors_detach_a_context_already_nested_in_a_sibling() -> None:
    shared = ValueError("shared leaf")
    sibling = ExceptionGroup("sibling", [shared])
    member = RuntimeError("boom")
    member.__context__ = shared

    with pytest.raises(BaseExceptionGroup) as info:
        raise_grouped_errors("grouped", sibling, member)

    assert member.__context__ is None
    assert set(info.value.exceptions) == {sibling, member}


def test_rollback_failed_acquire_groups_registration_and_release_failures(
    tmp_path: Path, mocker: MockerFixture
) -> None:
    # A failed acquisition can leave the descriptor owned while rollback also fails.
    lock = SoftFileLock(str(tmp_path / "a"))
    lock.acquire()
    registration_error = OSError("registration failed")
    release_error = OSError("release failed")
    mocker.patch.object(lock, "_register_context_descriptor", side_effect=registration_error, autospec=True)
    mocker.patch.object(lock, "_release_with_fork_tracking", side_effect=release_error, autospec=True)
    acquisition_error = RuntimeError("acquire failed")

    try:
        with pytest.raises(BaseExceptionGroup) as info:
            lock._rollback_failed_acquire(acquisition_error)
        assert set(info.value.exceptions) == {acquisition_error, registration_error, release_error}
    finally:
        mocker.stopall()
        lock.release(force=True)


@pytest.mark.timeout(5)
def test_append_exception_context_terminates_on_a_cycle() -> None:
    first = RuntimeError("first")
    second = RuntimeError("second")
    first.__context__ = second
    second.__context__ = first

    append_exception_context(first, ValueError("context"))

    assert first.__context__ is second
    assert second.__context__ is first
