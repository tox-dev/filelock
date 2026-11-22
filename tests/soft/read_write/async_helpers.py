from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Concatenate, Final, Generic, ParamSpec, TypeVar

from filelock import AsyncSoftReadWriteLock, SoftReadWriteLock, Timeout
from filelock._soft._read_write._generations import GenerationLog
from filelock._soft._read_write._storage import OsFiles

if TYPE_CHECKING:
    from collections.abc import Callable
    from concurrent.futures import Executor

    from pytest_mock import MockerFixture

    from filelock import LeaseCompromise


TASK_WAIT: Final[float] = 10


def make_async_lock(
    tmp_path: Path,
    name: str = "foo.lock",
    *,
    timeout: float = -1,
    blocking: bool = True,
    heartbeat_interval: float = 0.1,
    stale_threshold: float = 0.5,
    poll_interval: float = 0.02,
    loop: asyncio.AbstractEventLoop | None = None,
    executor: Executor | None = None,
    is_singleton: bool = False,
    on_compromise: Callable[[LeaseCompromise], None] | None = None,
) -> AsyncSoftReadWriteLock:
    return AsyncSoftReadWriteLock(
        str(tmp_path / name),
        timeout=timeout,
        blocking=blocking,
        is_singleton=is_singleton,
        heartbeat_interval=heartbeat_interval,
        stale_threshold=stale_threshold,
        poll_interval=poll_interval,
        loop=loop,
        executor=executor,
        on_compromise=on_compromise,
    )


def members(tmp_path: Path, name: str = "foo.lock") -> list[str]:
    lock_file = str(tmp_path / name)
    latest = GenerationLog(OsFiles(lock_file), lock_file, f"{lock_file}.rw").latest()
    holders = tmp_path / f"{name}.rw" / "holders"
    return sorted(latest.members) + (sorted(entry.name for entry in holders.iterdir()) if holders.is_dir() else [])


_Params = ParamSpec("_Params")


_Result = TypeVar("_Result")


class BackendGate(Generic[_Params, _Result]):
    # Cancel only after the executor has entered the backend.
    def __init__(
        self,
        mocker: MockerFixture,
        name: str,
        method: Callable[Concatenate[SoftReadWriteLock, _Params], _Result],
        *,
        fail_with: BaseException | None = None,
    ) -> None:
        self.started = asyncio.Event()
        # The event distinguishes backend completion from caller cancellation.
        self.finished = threading.Event()
        self._resume = threading.Event()
        self._loop = asyncio.get_running_loop()
        self._real = method
        self._pending = True
        mocker.patch.object(SoftReadWriteLock, name, autospec=True, side_effect=self._call(fail_with))

    def _call(self, fail_with: BaseException | None) -> Callable[Concatenate[SoftReadWriteLock, _Params], _Result]:
        def call(lock: SoftReadWriteLock, *args: _Params.args, **kwargs: _Params.kwargs) -> _Result:
            if not self._pending:
                return self._real(lock, *args, **kwargs)
            self._pending = False
            self._loop.call_soon_threadsafe(self.started.set)
            assert self._resume.wait(timeout=5)
            try:
                if fail_with is not None:
                    raise fail_with
                return self._real(lock, *args, **kwargs)
            finally:
                self.finished.set()

        return call

    def resume(self) -> None:
        self._resume.set()


async def read_and_probe(lock: AsyncSoftReadWriteLock) -> bool:
    async with lock.read_lock(timeout=TASK_WAIT):
        return await asyncio.to_thread(writer_gets_in, Path(lock.lock_file).parent)


def writer_gets_in(tmp_path: Path) -> bool:
    contender: Final[SoftReadWriteLock] = make_sync(tmp_path)
    try:
        contender.acquire_write(blocking=False)
    except Timeout:
        return False
    finally:
        contender.close()
    return True


def make_sync(tmp_path: Path) -> SoftReadWriteLock:
    return SoftReadWriteLock(
        str(tmp_path / "foo.lock"), is_singleton=False, heartbeat_interval=0.1, stale_threshold=0.5
    )


__all__ = [
    "TASK_WAIT",
    "BackendGate",
    "make_async_lock",
    "make_sync",
    "members",
    "read_and_probe",
    "writer_gets_in",
]
