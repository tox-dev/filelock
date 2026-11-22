from __future__ import annotations

import logging
from pathlib import Path, PurePath
from typing import TYPE_CHECKING

import pytest

from filelock import (
    AsyncFileLock,
    AsyncSoftFileLock,
    BaseAsyncFileLock,
    Timeout,
)
from tests.async_lock.context import ENTER

if TYPE_CHECKING:
    from tests.async_lock.context import Enter


@pytest.mark.parametrize("lock_type", [AsyncFileLock, AsyncSoftFileLock])
@pytest.mark.parametrize("path_type", [str, PurePath, Path])
@pytest.mark.parametrize("filename", ["a", "new/b", "new2/new3/c"])
@pytest.mark.parametrize("enter", ENTER)
@pytest.mark.asyncio
async def test_simple(
    lock_type: type[BaseAsyncFileLock],
    path_type: type[str | Path],
    filename: str,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    *,
    enter: Enter,
) -> None:
    caplog.set_level(logging.DEBUG)

    lock_path = tmp_path / filename
    lock = lock_type(path_type(lock_path))
    async with enter(lock) as locked:
        assert lock.is_locked
        assert lock is locked
    assert not lock.is_locked

    # A runtime with a lazy collector may finalize, mid-test, a lock an earlier test dropped.
    assert [(r.name, r.levelno, r.getMessage()) for r in caplog.records if str(lock_path) in r.getMessage()] == [
        ("filelock", logging.DEBUG, f"Attempting to acquire lock {id(lock)} on {lock_path}"),
        ("filelock", logging.DEBUG, f"Lock {id(lock)} acquired on {lock_path}"),
        ("filelock", logging.DEBUG, f"Attempting to release lock {id(lock)} on {lock_path}"),
        ("filelock", logging.DEBUG, f"Lock {id(lock)} released on {lock_path}"),
    ]
    assert logging.getLogger("filelock").level == logging.NOTSET


@pytest.mark.parametrize("lock_type", [AsyncFileLock, AsyncSoftFileLock])
@pytest.mark.asyncio
async def test_non_blocking(lock_type: type[BaseAsyncFileLock], tmp_path: Path) -> None:
    lock_path = tmp_path / "a"
    lock_1, lock_2 = lock_type(str(lock_path)), lock_type(str(lock_path))
    lock_3 = lock_type(str(lock_path), blocking=False)
    lock_4 = lock_type(str(lock_path), timeout=0)
    lock_5 = lock_type(str(lock_path), blocking=False, timeout=-1)

    await lock_1.acquire()
    assert lock_1.is_locked
    assert not lock_2.is_locked
    assert not lock_3.is_locked
    assert not lock_4.is_locked
    assert not lock_5.is_locked

    with pytest.raises(Timeout, match=r"The file lock '.*' could not be acquired."):
        await lock_2.acquire(blocking=False)
    assert not lock_2.is_locked
    assert lock_1.is_locked

    with pytest.raises(Timeout, match=r"The file lock '.*' could not be acquired."):
        await lock_3.acquire()
    assert not lock_3.is_locked
    assert lock_1.is_locked

    with pytest.raises(Timeout, match=r"The file lock '.*' could not be acquired."):
        async with lock_3:
            pass  # pragma: no cover  # __aenter__ raises Timeout, so the body never runs
    assert not lock_3.is_locked
    assert lock_1.is_locked

    with pytest.raises(Timeout, match=r"The file lock '.*' could not be acquired."):
        await lock_4.acquire()
    assert not lock_4.is_locked
    assert lock_1.is_locked

    with pytest.raises(Timeout, match=r"The file lock '.*' could not be acquired."):
        async with lock_4:
            pass  # pragma: no cover  # __aenter__ raises Timeout, so the body never runs
    assert not lock_4.is_locked
    assert lock_1.is_locked

    # blocking precedence over timeout
    with pytest.raises(Timeout, match=r"The file lock '.*' could not be acquired."):
        await lock_5.acquire()
    assert not lock_5.is_locked
    assert lock_1.is_locked

    with pytest.raises(Timeout, match=r"The file lock '.*' could not be acquired."):
        async with lock_5:
            pass  # pragma: no cover  # __aenter__ raises Timeout, so the body never runs
    assert not lock_5.is_locked
    assert lock_1.is_locked

    await lock_1.release()
    assert not lock_1.is_locked
    assert not lock_2.is_locked
    assert not lock_3.is_locked
    assert not lock_4.is_locked
    assert not lock_5.is_locked


@pytest.mark.parametrize(
    "lock_type",
    [pytest.param(AsyncFileLock, id="async"), pytest.param(AsyncSoftFileLock, id="soft")],
)
def test_sync_with_raises_not_implemented_error(lock_type: type[BaseAsyncFileLock], tmp_path: Path) -> None:
    # __exit__ must exist so Python can call it after __enter__ raises; without it AttributeError hides the real error
    with pytest.raises(NotImplementedError, match=r"async with"), lock_type(str(tmp_path / "test.lock")):
        pass  # pragma: no cover  # __enter__ raises NotImplementedError, so the body never runs
