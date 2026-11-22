from __future__ import annotations

import asyncio
import traceback
from errno import EIO
from typing import TYPE_CHECKING

import pytest

from filelock import (
    AsyncFileLock,
    AsyncSoftFileLock,
    BaseAsyncFileLock,
    ContextErrorPolicy,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from tests.async_lock.context import Enter


from tests.async_lock.context import ENTER


@pytest.mark.parametrize("enter", ENTER)
@pytest.mark.asyncio
async def test_context_group_detaches_release_context(
    tmp_path: Path,
    close_failure: tuple[Callable[[int], None], OSError, RuntimeError],
    *,
    enter: Enter,
) -> None:
    capture, release_error, release_cause = close_failure
    body_error = ValueError("body failed")
    body_cause = LookupError("body cause")
    lock = AsyncFileLock(
        str(tmp_path / "a"),
        thread_local=False,
        context_error_policy="group",
        close_error_policy="raise",
        on_acquired=capture,
    )
    with pytest.raises(ExceptionGroup) as info:
        async with enter(lock):
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


@pytest.mark.parametrize("enter", ENTER)
@pytest.mark.asyncio
async def test_context_group_renders_independent_leaves(
    tmp_path: Path,
    close_failure: tuple[Callable[[int], None], OSError, RuntimeError],
    *,
    enter: Enter,
) -> None:
    capture, release_error, _ = close_failure
    release_error.__cause__ = None
    release_error.__suppress_context__ = False
    body_error = ValueError("body failed")
    lock = AsyncFileLock(
        str(tmp_path / "a"),
        thread_local=False,
        context_error_policy="group",
        close_error_policy="raise",
        on_acquired=capture,
    )
    with pytest.raises(ExceptionGroup) as info:
        async with enter(lock):
            raise body_error
    group_rendering = "".join(traceback.format_exception(info.value))
    release_rendering = "".join(traceback.format_exception(release_error))
    assert (
        group_rendering.count("ValueError: body failed"),
        release_rendering.count("ValueError: body failed"),
        release_rendering.count("RuntimeError: release cause"),
        release_rendering.count("OSError: release failed"),
    ) == (1, 0, 0, 1)


@pytest.mark.parametrize("enter", ENTER)
@pytest.mark.asyncio
async def test_context_chain_keeps_release_error_with_body_in_context(
    tmp_path: Path,
    close_failure: tuple[Callable[[int], None], OSError, RuntimeError],
    *,
    enter: Enter,
) -> None:
    capture, release_error, _ = close_failure
    body_error = ValueError("body failed")
    lock = AsyncFileLock(
        str(tmp_path / "a"),
        thread_local=False,
        context_error_policy="chain",
        close_error_policy="raise",
        on_acquired=capture,
    )
    with pytest.raises(OSError, match="release failed") as info:
        async with enter(lock):
            raise body_error
    assert (info.value, release_error.__context__) == (release_error, body_error)


@pytest.mark.parametrize(
    "policy",
    [pytest.param("chain", id="chain"), pytest.param("group", id="group")],
)
@pytest.mark.parametrize("enter", ENTER)
@pytest.mark.asyncio
async def test_context_body_only_failure_propagates_body(
    tmp_path: Path, policy: ContextErrorPolicy, *, enter: Enter
) -> None:
    lock = AsyncSoftFileLock(str(tmp_path / "a"), context_error_policy=policy)
    body = ValueError("body failed")
    with pytest.raises(ValueError, match="body failed") as info:
        async with enter(lock):
            raise body
    assert info.value is body


@pytest.mark.parametrize(
    "policy",
    [pytest.param("chain", id="chain"), pytest.param("group", id="group")],
)
@pytest.mark.parametrize("enter", ENTER)
@pytest.mark.asyncio
async def test_context_release_only_failure_propagates_release(
    tmp_path: Path,
    close_failure: tuple[Callable[[int], None], OSError, RuntimeError],
    policy: ContextErrorPolicy,
    *,
    enter: Enter,
) -> None:
    capture, release_error, release_cause = close_failure
    lock = AsyncFileLock(
        str(tmp_path / "a"),
        thread_local=False,
        context_error_policy=policy,
        close_error_policy="raise",
        on_acquired=capture,
    )
    with pytest.raises(OSError, match="release failed") as info:
        async with enter(lock):
            pass
    assert (info.value, release_error.__context__, release_error.__cause__) == (release_error, None, release_cause)


@pytest.mark.asyncio
async def test_context_group_base_exception_leaf_is_base_group(
    tmp_path: Path, close_failure: tuple[Callable[[int], None], OSError, RuntimeError]
) -> None:
    capture, _, _ = close_failure
    lock = AsyncFileLock(
        str(tmp_path / "a"),
        thread_local=False,
        context_error_policy="group",
        close_error_policy="raise",
        on_acquired=capture,
    )
    with pytest.raises(BaseExceptionGroup) as info:
        async with lock:
            raise KeyboardInterrupt
    assert not isinstance(info.value, ExceptionGroup)
    assert [type(leaf) for leaf in info.value.exceptions] == [KeyboardInterrupt, OSError]


@pytest.mark.asyncio
async def test_context_group_preserves_user_release_cancellation_group(tmp_path: Path) -> None:
    body_error = ValueError("body failed")
    backend_cancellation = asyncio.CancelledError("backend canceled")
    backend_error = OSError(EIO, "backend failed")
    release_group = BaseExceptionGroup(
        "lock release cancellation and backend release both failed",
        (backend_cancellation, backend_error),
    )
    fail_release = True

    class GroupReleaseLock(BaseAsyncFileLock):
        def _acquire(self) -> None:
            self._context.lock_file_fd = 1

        def _release(self) -> None:
            nonlocal fail_release
            if fail_release:
                fail_release = False
                raise release_group
            self._context.lock_file_fd = None

    lock = GroupReleaseLock(tmp_path / "a", run_in_executor=False, context_error_policy="group")
    with pytest.raises(BaseExceptionGroup) as info:
        async with lock:
            raise body_error

    assert info.value.exceptions == (body_error, release_group)
    assert info.value.exceptions[1] is release_group
    assert release_group.exceptions == (backend_cancellation, backend_error)
    await lock.release()
