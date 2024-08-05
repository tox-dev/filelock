from __future__ import annotations

import logging
from pathlib import Path, PurePath

import pytest

from filelock import AsyncFileLock, AsyncSoftFileLock, BaseAsyncFileLock, Timeout


@pytest.mark.parametrize("lock_type", [AsyncFileLock, AsyncSoftFileLock])
@pytest.mark.parametrize("path_type", [str, PurePath, Path])
@pytest.mark.parametrize("filename", ["a", "new/b", "new2/new3/c"])
@pytest.mark.asyncio
async def test_simple(
    lock_type: type[BaseAsyncFileLock],
    path_type: type[str | Path],
    filename: str,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)

    # test lock creation by passing a `str`
    lock_path = tmp_path / filename
    lock = lock_type(path_type(lock_path))
    async with lock as locked:
        assert lock.is_locked
        assert lock is locked
    assert not lock.is_locked

    assert caplog.messages == [
        f"Attempting to acquire lock {id(lock)} on {lock_path}",
        f"Lock {id(lock)} acquired on {lock_path}",
        f"Attempting to release lock {id(lock)} on {lock_path}",
        f"Lock {id(lock)} released on {lock_path}",
    ]
    assert [r.levelno for r in caplog.records] == [logging.DEBUG, logging.DEBUG, logging.DEBUG, logging.DEBUG]
    assert [r.name for r in caplog.records] == ["filelock", "filelock", "filelock", "filelock"]
    assert logging.getLogger("filelock").level == logging.NOTSET


@pytest.mark.parametrize("lock_type", [AsyncFileLock, AsyncSoftFileLock])
@pytest.mark.parametrize("path_type", [str, PurePath, Path])
@pytest.mark.parametrize("filename", ["a", "new/b", "new2/new3/c"])
@pytest.mark.asyncio
async def test_acquire(
    lock_type: type[BaseAsyncFileLock],
    path_type: type[str | Path],
    filename: str,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)

    # test lock creation by passing a `str`
    lock_path = tmp_path / filename
    lock = lock_type(path_type(lock_path))
    async with await lock.acquire() as locked:
        assert lock.is_locked
        assert lock is locked
    assert not lock.is_locked

    assert caplog.messages == [
        f"Attempting to acquire lock {id(lock)} on {lock_path}",
        f"Lock {id(lock)} acquired on {lock_path}",
        f"Attempting to release lock {id(lock)} on {lock_path}",
        f"Lock {id(lock)} released on {lock_path}",
    ]
    assert [r.levelno for r in caplog.records] == [logging.DEBUG, logging.DEBUG, logging.DEBUG, logging.DEBUG]
    assert [r.name for r in caplog.records] == ["filelock", "filelock", "filelock", "filelock"]
    assert logging.getLogger("filelock").level == logging.NOTSET


@pytest.mark.parametrize("lock_type", [AsyncFileLock, AsyncSoftFileLock])
@pytest.mark.asyncio
async def test_non_blocking(lock_type: type[BaseAsyncFileLock], tmp_path: Path) -> None:
    # raises Timeout error when the lock cannot be acquired
    lock_path = tmp_path / "a"
    lock_1, lock_2 = lock_type(str(lock_path)), lock_type(str(lock_path))
    lock_3 = lock_type(str(lock_path), blocking=False)
    lock_4 = lock_type(str(lock_path), timeout=0)
    lock_5 = lock_type(str(lock_path), blocking=False, timeout=-1)

    # acquire lock 1
    await lock_1.acquire()
    assert lock_1.is_locked
    assert not lock_2.is_locked
    assert not lock_3.is_locked
    assert not lock_4.is_locked
    assert not lock_5.is_locked

    # try to acquire lock 2
    with pytest.raises(Timeout, match="The file lock '.*' could not be acquired."):
        await lock_2.acquire(blocking=False)
    assert not lock_2.is_locked
    assert lock_1.is_locked

    # try to acquire pre-parametrized `blocking=False` lock 3 with `acquire`
    with pytest.raises(Timeout, match="The file lock '.*' could not be acquired."):
        await lock_3.acquire()
    assert not lock_3.is_locked
    assert lock_1.is_locked

    # try to acquire pre-parametrized `blocking=False` lock 3 with context manager
    with pytest.raises(Timeout, match="The file lock '.*' could not be acquired."):
        async with lock_3:
            pass
    assert not lock_3.is_locked
    assert lock_1.is_locked

    # try to acquire pre-parametrized `timeout=0` lock 4 with `acquire`
    with pytest.raises(Timeout, match="The file lock '.*' could not be acquired."):
        await lock_4.acquire()
    assert not lock_4.is_locked
    assert lock_1.is_locked

    # try to acquire pre-parametrized `timeout=0` lock 4 with context manager
    with pytest.raises(Timeout, match="The file lock '.*' could not be acquired."):
        async with lock_4:
            pass
    assert not lock_4.is_locked
    assert lock_1.is_locked

    # blocking precedence over timeout
    # try to acquire pre-parametrized `timeout=-1,blocking=False` lock 5 with `acquire`
    with pytest.raises(Timeout, match="The file lock '.*' could not be acquired."):
        await lock_5.acquire()
    assert not lock_5.is_locked
    assert lock_1.is_locked

    # try to acquire pre-parametrized `timeout=-1,blocking=False` lock 5 with context manager
    with pytest.raises(Timeout, match="The file lock '.*' could not be acquired."):
        async with lock_5:
            pass
    assert not lock_5.is_locked
    assert lock_1.is_locked

    # release lock 1
    await lock_1.release()
    assert not lock_1.is_locked
    assert not lock_2.is_locked
    assert not lock_3.is_locked
    assert not lock_4.is_locked
    assert not lock_5.is_locked


@pytest.mark.parametrize("lock_type", [AsyncFileLock, AsyncSoftFileLock])
@pytest.mark.parametrize("thread_local", [True, False])
@pytest.mark.asyncio
async def test_non_executor(lock_type: type[BaseAsyncFileLock], thread_local: bool, tmp_path: Path) -> None:
    lock_path = tmp_path / "a"
    lock = lock_type(str(lock_path), thread_local=thread_local, run_in_executor=False)
    async with lock as locked:
        assert lock.is_locked
        assert lock is locked
    assert not lock.is_locked


@pytest.mark.asyncio
async def test_coroutine_function(tmp_path: Path) -> None:
    acquired = released = False

    class AioFileLock(BaseAsyncFileLock):
        async def _acquire(self) -> None:  # type: ignore[override]
            nonlocal acquired
            acquired = True
            self._context.lock_file_fd = 1

        async def _release(self) -> None:  # type: ignore[override]
            nonlocal released
            released = True
            self._context.lock_file_fd = None

    lock = AioFileLock(str(tmp_path / "a"))
    await lock.acquire()
    assert acquired
    assert not released
    await lock.release()
    assert acquired
    assert released
