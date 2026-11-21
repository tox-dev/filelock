from __future__ import annotations

import functools
import sqlite3
from typing import TYPE_CHECKING, Final, Literal, cast

import pytest

from filelock import AsyncReadWriteLock, ReadWriteLock, Timeout
from tests.read_write_helpers import OPPOSITE_MODE

if TYPE_CHECKING:
    import asyncio
    import threading
    from collections.abc import Callable

    from pytest_mock import MockerFixture


TASK_WAIT: Final[float] = 10


def assert_mode_held(lock_file: str, mode: Literal["read", "write"]) -> None:
    contender = ReadWriteLock(lock_file, is_singleton=False)
    acquire = getattr(contender, f"acquire_{OPPOSITE_MODE[mode]}")
    with pytest.raises(Timeout):
        acquire(blocking=False)
    contender.close()


async def can_write(lock: AsyncReadWriteLock) -> bool:
    try:
        await lock.acquire_write(blocking=False)
    except Timeout:
        return False
    await lock.release()
    return True


async def write_once(lock: AsyncReadWriteLock) -> None:
    async with lock.write_lock(timeout=TASK_WAIT):
        pass


def pause_rollback(
    mocker: MockerFixture,
    loop: asyncio.AbstractEventLoop,
    rollback_started: asyncio.Event,
    finish_rollback: threading.Event,
) -> None:
    def rollback(real_connection: sqlite3.Connection) -> None:
        loop.call_soon_threadsafe(rollback_started.set)
        finish_rollback.wait()
        real_connection.rollback()

    patch_connection(mocker, rollback=rollback)


def patch_connection(
    mocker: MockerFixture,
    *,
    execute: Callable[[sqlite3.Connection, str], sqlite3.Cursor] | None = None,
    rollback: Callable[[sqlite3.Connection], None] | None = None,
) -> None:
    real_connect = sqlite3.connect
    connection_count = 0

    def connect(
        database: str,
        *,
        factory: type[sqlite3.Connection],
        timeout: float,
    ) -> sqlite3.Connection:
        nonlocal connection_count
        real_connection = real_connect(
            database,
            check_same_thread=False,
            factory=factory,
            cached_statements=0,
            timeout=timeout,
        )
        connection_count += 1
        if connection_count < 2:
            return real_connection
        configuration: dict[str, Callable[[str], sqlite3.Cursor] | Callable[[], None]] = {}
        if execute is not None:
            configuration["execute.side_effect"] = functools.partial(execute, real_connection)
        if rollback is not None:
            configuration["rollback.side_effect"] = functools.partial(rollback, real_connection)
        connection = mocker.MagicMock(
            spec_set=type(real_connection),
            wraps=real_connection,
            database_identity=None,
            **configuration,
        )
        mocker.patch.object(
            type(connection),
            "in_transaction",
            new_callable=mocker.PropertyMock,
            create=True,
            side_effect=lambda: real_connection.in_transaction,
        )
        return cast("sqlite3.Connection", connection)

    mocker.patch("filelock._sqlite._transactions.connect", side_effect=connect, autospec=True)


def fail_rollback(
    mocker: MockerFixture,
    loop: asyncio.AbstractEventLoop,
    rollback_started: asyncio.Event,
    finish_rollback: threading.Event,
    *,
    prior_context: BaseException | None = None,
) -> sqlite3.OperationalError:
    rollback_error = sqlite3.OperationalError("rollback failed")
    rollback_error.__context__ = prior_context
    rollback_failed = False

    def rollback(real_connection: sqlite3.Connection) -> None:
        nonlocal rollback_failed
        if rollback_failed:
            real_connection.rollback()
            return
        rollback_failed = True
        loop.call_soon_threadsafe(rollback_started.set)
        assert finish_rollback.wait(timeout=5)
        raise rollback_error

    patch_connection(mocker, rollback=rollback)
    return rollback_error


__all__ = [
    "TASK_WAIT",
    "assert_mode_held",
    "can_write",
    "fail_rollback",
    "patch_connection",
    "pause_rollback",
    "write_once",
]
