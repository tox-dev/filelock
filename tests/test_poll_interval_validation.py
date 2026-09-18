from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Final, Literal, cast

import pytest

from filelock import AsyncFileLock, AsyncSoftFileLock, FileLock, SoftFileLock

if TYPE_CHECKING:
    from pathlib import Path


pytestmark: Final[pytest.MarkDecorator] = pytest.mark.filterwarnings("ignore::filelock.SoftFileLockLifetimeWarning")


def _set_poll_interval_sync(
    lock_type: type[FileLock | SoftFileLock],
    entry_point: Literal["constructor", "setter", "acquire"],
    lock: FileLock | SoftFileLock,
    value: float,
) -> None:
    if entry_point == "constructor":
        lock_type(lock.lock_file, poll_interval=value)
    elif entry_point == "setter":
        lock.poll_interval = value
    else:
        lock.acquire(poll_interval=value, timeout=0)


@pytest.mark.parametrize(
    "lock_type",
    [
        pytest.param(FileLock, id="native-sync"),
        pytest.param(SoftFileLock, id="soft-sync"),
        pytest.param(AsyncFileLock, id="native-async"),
        pytest.param(AsyncSoftFileLock, id="soft-async"),
    ],
)
@pytest.mark.parametrize(
    "entry_point",
    [pytest.param("constructor", id="constructor"), pytest.param("setter", id="setter")],
)
@pytest.mark.parametrize(
    ("bad_value", "error_type", "message"),
    [
        pytest.param(-1, ValueError, "finite and non-negative", id="negative-int"),
        pytest.param(-0.5, ValueError, "finite and non-negative", id="negative-float"),
        pytest.param(float("nan"), ValueError, "finite and non-negative", id="nan"),
        pytest.param(float("inf"), ValueError, "finite and non-negative", id="positive-infinity"),
        pytest.param(float("-inf"), ValueError, "finite and non-negative", id="negative-infinity"),
        pytest.param(True, TypeError, "poll_interval must be", id="true"),
        pytest.param(False, TypeError, "poll_interval must be", id="false"),
        pytest.param("5", TypeError, "poll_interval must be", id="string"),
    ],
)
def test_poll_interval_rejects_invalid_value(
    lock_type: type,
    entry_point: Literal["constructor", "setter"],
    bad_value: object,
    error_type: type[ValueError | TypeError],
    message: str,
    tmp_path: Path,
) -> None:
    lock_path = tmp_path / "test.lock"
    lock = lock_type(lock_path)
    with pytest.raises(error_type, match=message):
        if entry_point == "constructor":
            lock_type(lock_path, poll_interval=cast("float", bad_value))
        else:
            lock.poll_interval = cast("float", bad_value)


@pytest.mark.parametrize(
    "lock_type",
    [pytest.param(FileLock, id="native-sync"), pytest.param(SoftFileLock, id="soft-sync")],
)
@pytest.mark.parametrize(
    ("bad_value", "error_type", "message"),
    [
        pytest.param(-1, ValueError, "finite and non-negative", id="negative-int"),
        pytest.param(float("nan"), ValueError, "finite and non-negative", id="nan"),
        pytest.param("5", TypeError, "poll_interval must be", id="string"),
    ],
)
def test_poll_interval_rejects_invalid_acquire_arg(
    lock_type: type[FileLock | SoftFileLock],
    bad_value: object,
    error_type: type[ValueError | TypeError],
    message: str,
    tmp_path: Path,
) -> None:
    lock = lock_type(tmp_path / "test.lock")
    with pytest.raises(error_type, match=message):
        lock.acquire(poll_interval=cast("float", bad_value), timeout=0)


@pytest.mark.parametrize(
    "lock_type",
    [pytest.param(AsyncFileLock, id="native-async"), pytest.param(AsyncSoftFileLock, id="soft-async")],
)
@pytest.mark.parametrize(
    ("bad_value", "error_type", "message"),
    [
        pytest.param(-1, ValueError, "finite and non-negative", id="negative-int"),
        pytest.param(float("nan"), ValueError, "finite and non-negative", id="nan"),
    ],
)
def test_poll_interval_rejects_invalid_async_acquire_arg(
    lock_type: type,
    bad_value: object,
    error_type: type[ValueError | TypeError],
    message: str,
    tmp_path: Path,
) -> None:
    lock = lock_type(tmp_path / "test.lock", thread_local=False)

    async def _run() -> None:
        with pytest.raises(error_type, match=message):
            await lock.acquire(poll_interval=cast("float", bad_value), timeout=0)

    asyncio.run(_run())


@pytest.mark.parametrize(
    "lock_type",
    [pytest.param(FileLock, id="native-sync"), pytest.param(SoftFileLock, id="soft-sync")],
)
@pytest.mark.parametrize(
    "value",
    [pytest.param(0, id="zero-int"), pytest.param(0.0, id="zero-float"), pytest.param(0.05, id="positive-float")],
)
def test_poll_interval_accepts_non_negative(
    lock_type: type[FileLock | SoftFileLock],
    value: float,
    tmp_path: Path,
) -> None:
    lock = lock_type(tmp_path / "test.lock", poll_interval=value)
    assert lock.poll_interval == value
    lock.poll_interval = value
    assert lock.poll_interval == value
