from __future__ import annotations

import os
import sys
from errno import EACCES, ENOSYS, EWOULDBLOCK
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from collections.abc import Generator
    from pathlib import Path
    from types import BuiltinFunctionType, FrameType

    from pytest_mock import MockerFixture, MockType

    from filelock import UnixFileLock


def interrupt_unix_create(
    lock: UnixFileLock, error: type[BaseException] = KeyboardInterrupt
) -> None:  # pragma: needs fcntl
    def interrupt(
        frame: FrameType, event: str, operation: BuiltinFunctionType | None
    ) -> None:  # pragma: no cover  # profile hooks run with tracing disabled
        if (
            event == "c_return"
            and frame.f_code.co_name == "open_into"
            and (operation is os.open or getattr(operation, "__name__", None) == "extend")
        ):
            raise error

    previous = sys.getprofile()
    sys.setprofile(interrupt)
    try:
        with pytest.raises(error):
            lock.acquire()
    finally:
        sys.setprofile(previous)


@pytest.fixture
def group_writable_umask() -> Generator[None]:  # pragma: needs fcntl
    original_umask = os.umask(0o002)
    try:
        yield
    finally:
        os.umask(original_umask)


@pytest.fixture
def write_denied_lock_file(
    request: pytest.FixtureRequest, tmp_path: Path, mocker: MockerFixture
) -> None:  # pragma: needs fcntl
    # Another account can deny writes while a lease also blocks reads.
    behavior = getattr(request, "param", "deny")
    real_open = os.open

    def deny_write(path: str, flags: int, mode: int = 0o777, *, dir_fd: int | None = None) -> int:
        if not flags & os.O_CREAT and str(tmp_path / "test.lock") == path:
            if flags & os.O_RDWR:
                if behavior == "unlink":
                    (tmp_path / "test.lock").unlink()
                raise PermissionError(EACCES, "Permission denied", path)
            if behavior == "leased":
                raise BlockingIOError(EWOULDBLOCK, "Resource temporarily unavailable", path)
        return real_open(path, flags, mode, dir_fd=dir_fd)

    mocker.patch("os.open", side_effect=deny_write, autospec=True)


@pytest.fixture
def unsupported_flock(mocker: MockerFixture) -> MockType:  # pragma: needs fcntl
    return mocker.patch(
        "filelock._native._unix.fcntl.flock",
        side_effect=OSError(ENOSYS, "Function not implemented"),
        autospec=True,
    )
