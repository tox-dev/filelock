from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING, Final, Literal, TypedDict

import pytest

from filelock import (
    AsyncFileLock,
    AsyncSoftFileLease,
    AsyncSoftFileLock,
    BaseAsyncFileLock,
)

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


class _AsyncOptions(TypedDict, total=False):
    loop: asyncio.AbstractEventLoop
    executor: ThreadPoolExecutor
    run_in_executor: bool


_ASYNC_SINGLETON_TYPES: Final[list[pytest.ParameterSet]] = [
    pytest.param(AsyncFileLock, id="native"),
    pytest.param(AsyncSoftFileLock, id="soft"),
    pytest.param(AsyncSoftFileLease, id="lease"),
]


@pytest.fixture
def async_options() -> Iterator[_AsyncOptions]:
    loop = asyncio.new_event_loop()
    with ThreadPoolExecutor() as executor:
        yield {"loop": loop, "executor": executor, "run_in_executor": False}
    loop.close()


@pytest.mark.parametrize("lock_type", _ASYNC_SINGLETON_TYPES)
def test_async_singleton_reuses_the_instance_for_the_same_options(
    tmp_path: Path, lock_type: type[BaseAsyncFileLock], async_options: _AsyncOptions
) -> None:
    lock = lock_type(tmp_path / "a.lock", is_singleton=True, **async_options)
    assert lock_type(tmp_path / "a.lock", is_singleton=True, **async_options) is lock


@pytest.mark.parametrize("option", [pytest.param(name, id=name) for name in ("loop", "executor", "run_in_executor")])
@pytest.mark.parametrize("lock_type", _ASYNC_SINGLETON_TYPES)
def test_async_singleton_rejects_reuse_with_another_option(
    tmp_path: Path,
    lock_type: type[BaseAsyncFileLock],
    async_options: _AsyncOptions,
    option: Literal["loop", "executor", "run_in_executor"],
) -> None:
    _held = lock_type(tmp_path / "a.lock", is_singleton=True, **async_options)  # the cache only keeps it weakly
    del async_options[option]
    with pytest.raises(ValueError, match=rf"\n\t{option} \(existing lock has "):
        lock_type(tmp_path / "a.lock", is_singleton=True, **async_options)
