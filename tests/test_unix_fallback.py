from __future__ import annotations

import os
import subprocess  # ruff:ignore[suspicious-subprocess-import]  # a clean interpreter isolates the blocked fcntl import
import sys
import warnings
from errno import EACCES, EIO, ENOSYS
from textwrap import dedent
from typing import TYPE_CHECKING, Final

import pytest

from filelock import SoftFileLock, Timeout, UnixFileLock
from filelock._identity import host_name, process_start_token
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
def test_fchmod_enosys_is_not_a_missing_flock(tmp_path: Path, mocker: MockerFixture) -> None:  # pragma: needs fcntl
    mocker.patch("os.fchmod", side_effect=OSError(ENOSYS, "Function not implemented"))
    lock = UnixFileLock(tmp_path / "test.lock", mode=0o644)

    with warnings.catch_warnings(record=True) as recorded:
        warnings.simplefilter("always")
        with pytest.raises(OSError, match="Function not implemented"):
            lock.acquire()

    assert not recorded
    assert not isinstance(lock, SoftFileLock)


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
    expected = [str(os.getpid()), host_name()]
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
@pytest.mark.filterwarnings("ignore:flock not supported on this filesystem:UserWarning")
@pytest.mark.usefixtures("unsupported_flock")
def test_fallback_removes_placeholder_it_created(tmp_path: Path) -> None:  # pragma: needs fcntl
    lock = UnixFileLock(tmp_path / "test.lock")

    lock.acquire(timeout=0)
    assert lock.is_locked
    lock.release()


@NEEDS_FCNTL
@pytest.mark.filterwarnings("ignore:flock not supported on this filesystem:UserWarning")
@pytest.mark.usefixtures("unsupported_flock")
def test_fallback_keeps_peer_soft_marker(tmp_path: Path) -> None:  # pragma: needs fcntl
    lock_path = tmp_path / "test.lock"
    holder = UnixFileLock(lock_path)
    holder.acquire()
    marker = lock_path.stat().st_ino, lock_path.read_bytes()

    with pytest.raises(Timeout):
        UnixFileLock(lock_path).acquire(timeout=0)
    assert (lock_path.stat().st_ino, lock_path.read_bytes()) == marker
    holder.release()


@NEEDS_FCNTL
@pytest.mark.filterwarnings("ignore:flock not supported on this filesystem:UserWarning")
def test_fallback_spares_a_peer_marker_that_replaced_its_placeholder(  # pragma: needs fcntl
    tmp_path: Path, mocker: MockerFixture
) -> None:
    lock_path: Final = tmp_path / "test.lock"
    peer_marker: Final = f"{os.getpid()}\n{host_name()}\n"
    lstat: Final = os.lstat
    stalled: list[bool] = []

    def flock_missing(fd: int, operation: int) -> None:
        stalled.append(False)
        raise OSError(ENOSYS, f"Function not implemented for {fd} with {operation}")

    def lstat_then_peer_replaces_placeholder(path: str, *, dir_fd: int | None = None) -> os.stat_result:
        found: Final = lstat(path, dir_fd=dir_fd)
        if stalled == [False] and os.fspath(path) == os.fspath(lock_path):
            # The cleanup stalls past the grace a waiting soft lock gives an empty file, which evicts the placeholder
            # and publishes its own marker.
            stalled[0] = True
            lock_path.unlink()
            lock_path.write_text(peer_marker, encoding="utf-8")
        return found

    mocker.patch("filelock._unix.fcntl.flock", side_effect=flock_missing)
    mocker.patch("os.lstat", side_effect=lstat_then_peer_replaces_placeholder)
    with pytest.raises(Timeout):
        UnixFileLock(lock_path).acquire(timeout=0)
    mocker.stopall()

    # Where os.link cannot restore it, the peer's marker waits under a break name, but it is never deleted.
    assert peer_marker in [path.read_text(encoding="utf-8") for path in tmp_path.glob("test.lock*")]


@NEEDS_FCNTL
@pytest.mark.filterwarnings("ignore:flock not supported on this filesystem:UserWarning")
@pytest.mark.usefixtures("unsupported_flock", "write_denied_lock_file")
def test_fallback_waits_on_a_peer_marker_it_cannot_write(tmp_path: Path) -> None:  # pragma: needs fcntl
    lock_path = tmp_path / "test.lock"
    holder = UnixFileLock(lock_path)
    holder.acquire()

    with pytest.raises(Timeout):
        UnixFileLock(lock_path).acquire(timeout=0)
    holder.release()
    with (contender := UnixFileLock(lock_path)).acquire(timeout=0):
        assert contender.is_locked


@NEEDS_FCNTL
@pytest.mark.usefixtures("write_denied_lock_file")
def test_lock_file_it_cannot_write_stays_denied_where_flock_works(tmp_path: Path) -> None:  # pragma: needs fcntl
    (lock_path := tmp_path / "test.lock").touch()

    with pytest.raises(PermissionError):
        UnixFileLock(lock_path).acquire(timeout=0)


@NEEDS_FCNTL
@pytest.mark.parametrize("write_denied_lock_file", [True], ids=["unlinked-under-the-denial"], indirect=True)
@pytest.mark.usefixtures("write_denied_lock_file")
def test_lock_file_unlinked_under_a_denied_open_is_retried(tmp_path: Path) -> None:  # pragma: needs fcntl
    (lock_path := tmp_path / "test.lock").touch()

    with (lock := UnixFileLock(lock_path)).acquire(timeout=1):
        assert lock.is_locked


@pytest.fixture
def write_denied_lock_file(
    request: pytest.FixtureRequest, tmp_path: Path, mocker: MockerFixture
) -> None:  # pragma: needs fcntl
    # Stand in for a marker another account owns: an O_RDWR open is refused, a read works.
    unlink_under_denial = getattr(request, "param", False)
    real_open = os.open

    def deny_write(path: str, flags: int, mode: int = 0o777, *, dir_fd: int | None = None) -> int:
        if flags & os.O_RDWR and not flags & os.O_CREAT and str(tmp_path / "test.lock") == path:
            if unlink_under_denial:
                (tmp_path / "test.lock").unlink()
            raise PermissionError(EACCES, "Permission denied", path)
        return real_open(path, flags, mode, dir_fd=dir_fd)

    mocker.patch("os.open", side_effect=deny_write)


@NEEDS_FCNTL  # pragma: needs fcntl
def test_trailing_slash_path_raises_instead_of_retrying(tmp_path: Path) -> None:
    # macOS reports ENOENT for a create through a trailing slash, Linux EISDIR; neither is contention.
    with pytest.raises((FileNotFoundError, IsADirectoryError)):
        UnixFileLock(f"{tmp_path / 'missing'}{os.sep}").acquire(timeout=0)


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
