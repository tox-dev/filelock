from __future__ import annotations

import os
import socket
import subprocess  # ruff:ignore[suspicious-subprocess-import]  # a clean interpreter isolates the blocked fcntl import
import sys
from errno import EIO, ENOSYS
from textwrap import dedent
from typing import TYPE_CHECKING, Final

import pytest

from filelock import SoftFileLock, UnixFileLock
from filelock._identity import process_start_token
from tests.capability_marks import NEEDS_FCNTL

if TYPE_CHECKING:
    from pathlib import Path
    from unittest.mock import MagicMock

    from pytest_mock import MockerFixture


@NEEDS_FCNTL
def test_import_without_fcntl_uses_soft_aliases_and_descriptor_errors() -> None:  # pragma: needs fcntl
    script: Final[str] = dedent(
        """
        import json
        import sys
        import warnings
        from collections.abc import Callable
        from errno import ENOSYS

        sys.modules["fcntl"] = None
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            import filelock

        def operation_errno(operation: Callable[[int], bool | None]) -> int | None:
            try:
                operation(-1)
            except OSError as exception:
                return exception.errno
            return None

        print(json.dumps({
            "aliases": [
                filelock.FileLock is filelock.SoftFileLock,
                filelock.AsyncFileLock is filelock.AsyncSoftFileLock,
            ],
            "errors": [
                operation_errno(filelock.lock_descriptor) == ENOSYS,
                operation_errno(filelock.unlock_descriptor) == ENOSYS,
            ],
            "warnings": [str(item.message) for item in caught],
        }, sort_keys=True))
        """,
    )
    result: Final[subprocess.CompletedProcess[str]] = subprocess.run(
        [sys.executable, "-c", script], check=True, capture_output=True, text=True
    )

    assert (result.stdout, result.stderr) == (
        '{"aliases": [true, true], "errors": [true, true], "warnings": ["only soft file lock is available"]}\n',
        "",
    )


@NEEDS_FCNTL
@pytest.mark.usefixtures("unsupported_flock")
def test_fallback_emits_warning(tmp_path: Path) -> None:  # pragma: needs fcntl
    lock = UnixFileLock(tmp_path / "test.lock")

    with pytest.warns(UserWarning, match="flock not supported on this filesystem, falling back to SoftFileLock"):
        lock.acquire()
    lock.release()


@NEEDS_FCNTL
@pytest.mark.filterwarnings("ignore:flock not supported on this filesystem:UserWarning")
@pytest.mark.usefixtures("unsupported_flock")
def test_fallback_swaps_to_soft(tmp_path: Path) -> None:  # pragma: needs fcntl
    lock = UnixFileLock(tmp_path / "test.lock")

    with lock:
        assert lock.is_locked
        assert isinstance(lock, SoftFileLock)


@NEEDS_FCNTL
@pytest.mark.filterwarnings("ignore:flock not supported on this filesystem:UserWarning")
@pytest.mark.usefixtures("unsupported_flock")
def test_fallback_writes_pid_and_hostname(tmp_path: Path) -> None:  # pragma: needs fcntl
    lock_path = tmp_path / "test.lock"

    with UnixFileLock(lock_path):
        lines = lock_path.read_text(encoding="utf-8").splitlines()
    expected = [str(os.getpid()), socket.gethostname()]
    if (token := process_start_token(os.getpid())) is not None:  # pragma: no branch  # CI always exposes a start time
        expected.append(str(token))
    assert lines == expected


@NEEDS_FCNTL
@pytest.mark.filterwarnings("ignore:flock not supported on this filesystem:UserWarning")
@pytest.mark.usefixtures("unsupported_flock")
def test_fallback_release_unlinks_file(tmp_path: Path) -> None:  # pragma: needs fcntl
    lock_path = tmp_path / "test.lock"
    lock = UnixFileLock(lock_path)

    lock.acquire()
    assert lock_path.exists()
    lock.release()
    assert not lock_path.exists()


@NEEDS_FCNTL
@pytest.mark.filterwarnings("ignore:flock not supported on this filesystem:UserWarning")  # pragma: needs fcntl
def test_fallback_subsequent_acquire_skips_flock(tmp_path: Path, unsupported_flock: MagicMock) -> None:
    lock = UnixFileLock(tmp_path / "test.lock")

    lock.acquire()
    lock.release()
    unsupported_flock.reset_mock()

    lock.acquire()
    lock.release()
    unsupported_flock.assert_not_called()


@NEEDS_FCNTL
@pytest.mark.filterwarnings("ignore:flock not supported on this filesystem:UserWarning")
@pytest.mark.usefixtures("unsupported_flock")
def test_fallback_reentrant_locking(tmp_path: Path) -> None:  # pragma: needs fcntl
    lock = UnixFileLock(tmp_path / "test.lock")

    with lock:
        with lock:
            assert lock.is_locked
        assert lock.is_locked
    assert not lock.is_locked


@NEEDS_FCNTL
def test_release_suppresses_eio_on_close(tmp_path: Path, mocker: MockerFixture) -> None:  # pragma: needs fcntl
    lock = UnixFileLock(tmp_path / "test.lock")
    lock.acquire()

    real_close = os.close  # capture before the patch so the fd still closes for real
    fd_to_fail = lock._context.lock_file_fd  # _release() nulls this before closing, so read it now

    def _close_eio(fd: int) -> None:
        real_close(fd)
        if fd == fd_to_fail:  # pragma: no branch  # only the lock's own descriptor closes while the patch is live
            raise OSError(EIO, "Input/output error")

    mocker.patch("filelock._unix.os.close", side_effect=_close_eio)
    lock.release()
    assert not lock.is_locked


@NEEDS_FCNTL  # pragma: needs fcntl
def test_acquire_flock_error_clears_pending_descriptor(tmp_path: Path, mocker: MockerFixture) -> None:
    lock = UnixFileLock(tmp_path / "test.lock")
    mocker.patch(
        "filelock._unix.fcntl.flock",
        side_effect=[OSError(EIO, "Input/output error"), None, None],
    )

    with pytest.raises(OSError, match="Input/output error"):
        lock.acquire(timeout=0)
    assert not lock.is_locked

    lock.acquire(timeout=0)
    assert lock.is_locked
    lock.release()
    assert not lock.is_locked


@pytest.fixture
def unsupported_flock(mocker: MockerFixture) -> MagicMock:  # pragma: needs fcntl
    return mocker.patch("filelock._unix.fcntl.flock", side_effect=OSError(ENOSYS, "Function not implemented"))
