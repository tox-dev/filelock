from __future__ import annotations

from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Final

import pytest

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Callable
    from contextlib import AbstractAsyncContextManager

    from filelock import BaseAsyncFileLock

    Enter = Callable[[BaseAsyncFileLock], AbstractAsyncContextManager[BaseAsyncFileLock]]


def _enter_lock(
    lock: BaseAsyncFileLock,
) -> AbstractAsyncContextManager[BaseAsyncFileLock]:
    return lock


@asynccontextmanager
async def _enter_proxy(lock: BaseAsyncFileLock) -> AsyncGenerator[BaseAsyncFileLock]:
    async with await lock.acquire() as locked:
        yield locked


ENTER: Final[list[pytest.ParameterSet]] = [
    pytest.param(_enter_lock, id="direct"),
    pytest.param(_enter_proxy, id="proxy"),
]


__all__ = [
    "ENTER",
    "Enter",
]
