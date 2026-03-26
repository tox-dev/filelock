"""Regression tests for PR #518.

Covers two bugs fixed in BaseAsyncFileLock:

1. Missing ``__exit__`` — without it, using ``with`` on an async lock raised
   ``AttributeError`` instead of a clear ``NotImplementedError``, because Python
   calls ``__exit__`` to clean up after ``__enter__`` raises, but the method
   did not exist on the class.

2. Stored event loop in ``__del__`` — the old implementation called
   ``asyncio.get_running_loop()`` unconditionally in ``__del__``, which raises
   ``RuntimeError`` when no loop is running (common during garbage collection
   after the loop has been closed).
"""

from __future__ import annotations

import asyncio
import gc
from typing import TYPE_CHECKING

import pytest

from filelock import AsyncFileLock, AsyncSoftFileLock, BaseAsyncFileLock

if TYPE_CHECKING:
    from pathlib import Path

# ── Bug 1 : missing __exit__ ──────────────────────────────────────────────────


@pytest.mark.parametrize("lock_type", [AsyncFileLock, AsyncSoftFileLock])
def test_sync_with_raises_not_implemented_error_not_attribute_error(
    lock_type: type[BaseAsyncFileLock],
    tmp_path: Path,
) -> None:
    """Using ``with`` (sync) on an async lock must raise ``NotImplementedError``.

    **Regression:** before this fix, ``__exit__`` was missing on
    ``BaseAsyncFileLock``.  When ``__enter__`` raised ``NotImplementedError``,
    Python tried to call ``__exit__`` to handle the exception — but the method
    did not exist, so an ``AttributeError`` was raised *instead*, masking the
    real problem and confusing users.

    The correct behaviour is a clear ``NotImplementedError`` with a message
    telling the user to use ``async with`` instead.
    """
    lock = lock_type(str(tmp_path / "test.lock"))

    # Must raise NotImplementedError, NOT AttributeError
    with pytest.raises(NotImplementedError, match=r"async with"), lock:  # sync context manager — must be rejected
        pass  # pragma: no cover


@pytest.mark.parametrize("lock_type", [AsyncFileLock, AsyncSoftFileLock])
def test_exit_method_exists_on_async_lock(lock_type: type[BaseAsyncFileLock], tmp_path: Path) -> None:
    """``__exit__`` must be defined on async lock classes.

    **Regression:** ``__enter__`` existed (raising ``NotImplementedError``) but
    ``__exit__`` was missing, so Python could not call it to clean up after the
    ``NotImplementedError`` from ``__enter__``.
    """
    lock = lock_type(str(tmp_path / "test.lock"))
    assert hasattr(lock, "__exit__"), (
        f"{lock_type.__name__} must define __exit__ so that `with` raises "
        "NotImplementedError cleanly instead of AttributeError"
    )
    assert callable(lock.__exit__)


@pytest.mark.parametrize("lock_type", [AsyncFileLock, AsyncSoftFileLock])
def test_exit_raises_not_implemented_error(lock_type: type[BaseAsyncFileLock], tmp_path: Path) -> None:
    """Calling ``__exit__`` directly must also raise ``NotImplementedError``."""
    lock = lock_type(str(tmp_path / "test.lock"))
    with pytest.raises(NotImplementedError, match=r"async with"):
        lock.__exit__(None, None, None)


# ── Bug 2 : stored event loop in __del__ ─────────────────────────────────────


@pytest.mark.parametrize("lock_type", [AsyncFileLock, AsyncSoftFileLock])
def test_del_after_loop_close_does_not_raise(
    lock_type: type[BaseAsyncFileLock],
    tmp_path: Path,
) -> None:
    """``__del__`` must not raise when the event loop has been closed.

    **Regression:** the old ``__del__`` called ``asyncio.get_running_loop()``
    unconditionally.  That method raises ``RuntimeError`` when no loop is
    running — which is exactly the situation during garbage collection after a
    loop has been closed.

    This test runs in a dedicated thread so it can create and close its own
    event loop without interfering with pytest-asyncio's loop.
    """
    import threading

    errors: list[Exception] = []

    def _run() -> None:
        lock = lock_type(str(tmp_path / "test.lock"))
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(lock.acquire())
            loop.run_until_complete(lock.release(force=True))
        finally:
            loop.close()
            asyncio.set_event_loop(None)

        # __del__ called with no running loop — must not raise RuntimeError
        try:
            lock.__del__()
        except RuntimeError as exc:
            errors.append(exc)

    t = threading.Thread(target=_run)
    t.start()
    t.join(timeout=10)
    assert not t.is_alive(), "Thread did not finish in time"
    assert not errors, (
        f"__del__ raised RuntimeError after loop was closed: {errors[0]!r}\n"
        "This is a regression of the stored-loop bug fixed in PR #518."
    )


@pytest.mark.parametrize("lock_type", [AsyncFileLock, AsyncSoftFileLock])
def test_gc_after_loop_close_does_not_raise(
    lock_type: type[BaseAsyncFileLock],
    tmp_path: Path,
) -> None:
    """GC after loop close must not raise RuntimeError.

    Real-world scenario: the lock is acquired, the loop is closed, and the
    object is garbage-collected with no running loop — ``__del__`` must not
    raise ``RuntimeError`` in that situation.
    """
    import threading

    errors: list[Exception] = []

    def _run() -> None:
        lock_path = tmp_path / "gc_test.lock"
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        lk = lock_type(str(lock_path))
        try:
            loop.run_until_complete(lk.acquire())
            loop.run_until_complete(lk.release(force=True))
        finally:
            loop.close()
            asyncio.set_event_loop(None)

        # lk still in scope but loop is gone — force GC
        try:
            del lk
            gc.collect()
        except RuntimeError as exc:
            errors.append(exc)

    t = threading.Thread(target=_run)
    t.start()
    t.join(timeout=10)
    assert not t.is_alive(), "Thread did not finish in time"
    assert not errors, (
        f"gc.collect() raised RuntimeError after loop close: {errors[0]!r}\n"
        "This is a regression of the stored-loop bug fixed in PR #518."
    )
