from __future__ import annotations

import os
import sys
from contextlib import contextmanager
from errno import EINTR
from typing import TYPE_CHECKING, Final

import pytest

from filelock import SoftFileLock
from filelock._identity import host_name, process_start_token
from tests.conftest import held_after_reclaim

if TYPE_CHECKING:
    from collections.abc import Generator
    from pathlib import Path

    from pytest_mock import MockerFixture


WINDOWS_ONLY: Final[pytest.MarkDecorator] = pytest.mark.skipif(sys.platform != "win32", reason="windows-only")


HOST: Final[str] = host_name()


DEAD_PID: Final[int] = 2**22 + 1


def assert_self_heals(lock_path: Path) -> None:
    with held_after_reclaim(SoftFileLock(lock_path)) as lock:
        assert lock.is_locked


def assert_times_out(lock_path: Path, *, timeout: float = 0.1) -> None:
    with pytest.raises(TimeoutError):
        SoftFileLock(lock_path, timeout=timeout).acquire()


REQUIRES_START_TOKEN: Final[pytest.MarkDecorator] = pytest.mark.skipif(
    process_start_token(os.getpid()) is None, reason="platform exposes no proven process start time"
)


def recovery_name(lock: Path, pid: int = DEAD_PID) -> Path:  # pragma: win32 no cover
    return lock.with_name(f"test.lock.break.{pid}.{'0' * 32}")


def live_record(record: str) -> str:  # pragma: win32 no cover
    return record.replace(str(DEAD_PID), str(os.getpid()))


def break_contents(lock: Path) -> list[str]:  # pragma: win32 no cover
    return [path.read_text(encoding="utf-8") for path in lock.parent.glob(f"{lock.name}.break.*")]


def hold_reports_foreign_identity(mocker: MockerFixture) -> None:
    # A replaced marker and the held descriptor have different inode numbers.
    mocker.patch(
        "os.fstat",
        return_value=os.stat_result((0, 999, 1, 0, 0, 0, 0, 0, 0, 0)),
        autospec=True,
    )


@contextmanager
def close_after_commit(mocker: MockerFixture, lock: SoftFileLock) -> Generator[tuple[OSError, list[int]]]:
    real_close = os.close
    close_error = OSError(EINTR, "close failed")
    attempts: list[int] = []
    # The global os.close patch can also intercept unrelated garbage-collection finalizers.
    released: list[int | None] = []
    real_mark_released = lock._mark_descriptor_released

    def mark_released() -> None:
        context = lock._context
        released.append(context.pending_lock_file_fd if context.lock_file_fd is None else context.lock_file_fd)
        real_mark_released()

    def close(fd: int) -> None:
        real_close(fd)
        if fd not in released:
            return
        attempts.append(fd)
        raise close_error

    mocker.patch.object(lock, "_mark_descriptor_released", side_effect=mark_released, autospec=True)
    close_mock = mocker.patch("os.close", side_effect=close, autospec=True)
    try:
        yield close_error, attempts
    finally:
        mocker.stop(close_mock)


def release_over_a_successor_marker(lock_path: Path) -> SoftFileLock:  # pragma: needs unlink-open-file
    holder: Final[SoftFileLock] = SoftFileLock(lock_path)
    holder.acquire()
    lock_path.unlink()
    lock_path.write_text(holder_record(DEAD_PID), encoding="utf-8")
    return holder


def holder_record(pid: int, *, host: str = HOST, start: int | None = None) -> str:
    return f"{pid}\n{host}\n" if start is None else f"{pid}\n{host}\n{start}\n"


__all__ = [
    "DEAD_PID",
    "HOST",
    "REQUIRES_START_TOKEN",
    "WINDOWS_ONLY",
    "assert_self_heals",
    "assert_times_out",
    "break_contents",
    "close_after_commit",
    "hold_reports_foreign_identity",
    "holder_record",
    "live_record",
    "recovery_name",
    "release_over_a_successor_marker",
]
