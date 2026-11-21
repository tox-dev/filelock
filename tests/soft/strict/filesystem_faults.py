from __future__ import annotations

import os
import stat
import sys
import time
from errno import EIO
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import Callable

    from pytest_mock import MockerFixture, MockType


PathValue = str | bytes | os.PathLike[str] | os.PathLike[bytes]


class Inspect(Protocol):
    def __call__(self, path: PathValue, **options: int | bool | None) -> os.stat_result: ...


def fail_directory_close_after(  # pragma: needs dir-fd
    mocker: MockerFixture, function: str, prefix: str, message: str
) -> None:
    # An unrelated directory can close first on filesystems without dir_fd.
    real_close = os.close
    real_fstat = os.fstat
    real_function = getattr(os, function)
    armed = False

    def arm_after(*paths: PathValue, **options: int | bool | None) -> None:
        nonlocal armed
        real_function(*paths, **options)
        armed = armed or Path(os.fsdecode(paths[-1])).name.startswith(prefix)

    def fail_directory_close(fd: int) -> None:
        nonlocal armed
        is_directory = stat.S_ISDIR(real_fstat(fd).st_mode)
        real_close(fd)
        if armed and is_directory:
            armed = False
            raise OSError(EIO, message)

    mocker.patch(f"os.{function}", side_effect=arm_after, autospec=True)
    mocker.patch("os.close", side_effect=fail_directory_close, autospec=True)


def patch_strict_clock(
    mocker: MockerFixture,
    module: str,
    *,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    wall: Callable[[], float] = time.time,
) -> None:
    mocker.patch(
        f"filelock._soft._strict.{module}.time", new=SimpleNamespace(monotonic=monotonic, sleep=sleep, time=wall)
    )


def open_raising_for(claim: Path, error: OSError, mocker: MockerFixture) -> MockType:
    real_open = os.open

    def failing_open(path: PathValue, flags: int, mode: int = 0o777, *, dir_fd: int | None = None) -> int:
        if Path(os.fsdecode(path)).name == claim.name:
            raise error
        # Only the directory walk bypasses the target claim.
        return real_open(path, flags, mode, dir_fd=dir_fd)  # pragma: needs dir-fd

    route_record_opens_through_os_open(mocker)
    return mocker.patch("os.open", side_effect=failing_open, autospec=True)


def route_record_opens_through_os_open(mocker: MockerFixture, *, claims: bool = True) -> None:
    # Windows uses NtCreateFile for claims and the sentinel, which an os.open fault would miss.
    if sys.platform == "win32":  # pragma: win32 cover
        if claims:
            mocker.patch(
                "filelock._soft._strict._scan.open_deletable_for_reading",
                side_effect=lambda path: os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0)),
                autospec=True,
            )
        mocker.patch(
            "filelock._soft._strict._scan.open_read_without_oplock_wait",
            side_effect=lambda path, **_: os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0)),
            autospec=True,
        )


def mock_legacy_volume(mocker: MockerFixture, failure: str | None = None) -> None:  # pragma: win32 cover
    assert sys.platform == "win32"
    import ctypes

    import filelock._filesystem._windows._handles as handles
    from filelock._soft._strict import _windows as strict_windows

    set_disposition = handles.try_set_delete_disposition

    def legacy_disposition(handle: int, *, published: bool = False) -> bool:
        if published:
            ctypes.set_last_error(50)  # ERROR_NOT_SUPPORTED
            return False
        if failure == "private-cleanup":
            raise OSError(EIO, failure)
        return set_disposition(handle)

    mocker.patch.object(handles, "try_set_delete_disposition", side_effect=legacy_disposition, autospec=True)
    if failure == "rename":
        mocker.patch.object(strict_windows, "rename_open_file", side_effect=OSError(EIO, failure), autospec=True)


__all__ = [
    "Inspect",
    "PathValue",
    "fail_directory_close_after",
    "mock_legacy_volume",
    "open_raising_for",
    "patch_strict_clock",
    "route_record_opens_through_os_open",
]
