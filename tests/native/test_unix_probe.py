from __future__ import annotations

import os
import stat
import sys
import warnings
from errno import EACCES, EBADF, EIO, ENOSYS
from typing import TYPE_CHECKING

import pytest

from filelock import SoftFileLock, UnixFileLock
from filelock._filesystem import exchange_names
from tests.capability_marks import NEEDS_FCNTL, NEEDS_STALE_RECLAIM
from tests.native.conftest import interrupt_unix_create

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path
    from types import BuiltinFunctionType, FrameType

    from pytest_mock import MockerFixture


@NEEDS_FCNTL
def test_interrupted_unix_create_keeps_native_file(tmp_path: Path) -> None:  # pragma: needs fcntl
    lock_path = tmp_path / "test.lock"
    interrupt_unix_create(UnixFileLock(lock_path, mode=0o600))
    assert lock_path.exists()
    with UnixFileLock(lock_path, timeout=0) as lock:
        assert lock.is_locked


@NEEDS_FCNTL
@pytest.mark.parametrize(
    "anonymous", [pytest.param(getattr(os, "O_TMPFILE", 0), id="default"), pytest.param(0, id="named")]
)
def test_interrupted_probe_leaves_directory_empty(  # pragma: needs fcntl
    tmp_path: Path, mocker: MockerFixture, anonymous: int
) -> None:
    mocker.patch.object(os, "O_TMPFILE", anonymous, create=True)
    real_open = os.open
    opened: list[int] = []

    def open_probe(path: str, flags: int, mode: int = 0o777) -> int:
        fd = real_open(path, flags, mode)
        opened.append(fd)
        return fd

    mocker.patch("os.open", autospec=True, side_effect=open_probe)
    lock = UnixFileLock(tmp_path / "test.lock")
    interrupt_unix_create(lock)
    assert not lock.is_locked
    assert list(tmp_path.iterdir()) == []
    with pytest.raises(OSError, match=os.strerror(EBADF)):
        os.fstat(opened[0])


@NEEDS_FCNTL
def test_named_probe_is_unlinked_before_flock(tmp_path: Path, mocker: MockerFixture) -> None:  # pragma: needs fcntl
    assert sys.platform != "win32"
    mocker.patch.object(os, "O_TMPFILE", 0, create=True)
    links: list[int] = []

    def unsupported(fd: int, _operation: int) -> None:
        links.append(os.fstat(fd).st_nlink)
        raise OSError(ENOSYS, "no flock")

    mocker.patch("filelock._native._unix.fcntl.flock", autospec=True, side_effect=unsupported)
    with pytest.warns(UserWarning, match="falling back"), UnixFileLock(tmp_path / "test.lock") as lock:
        assert lock.pid == os.getpid()
    assert links == [0]


@NEEDS_FCNTL
@pytest.mark.parametrize(
    "cleanup", [pytest.param("_unlink_probe", id="unlink"), pytest.param("_close_probe", id="close")]
)
def test_interrupted_probe_cleanup_retires_its_file(  # pragma: needs fcntl
    tmp_path: Path, mocker: MockerFixture, cleanup: str
) -> None:
    mocker.patch.object(os, "O_TMPFILE", 0, create=True)
    opened = mocker.spy(os, "open")

    def interrupt(
        frame: FrameType, event: str, _operation: BuiltinFunctionType | None
    ) -> None:  # pragma: no cover  # profile hooks run with tracing disabled
        if event == "call" and frame.f_code.co_name == cleanup:
            raise KeyboardInterrupt

    lock = UnixFileLock(tmp_path / "test.lock")
    previous = sys.getprofile()
    try:
        sys.setprofile(interrupt)
        with pytest.raises(KeyboardInterrupt):
            lock.acquire(timeout=0)
    finally:
        sys.setprofile(previous)
    assert list(tmp_path.iterdir()) == []
    with pytest.raises(OSError, match=os.strerror(EBADF)):
        os.fstat(opened.spy_return)


@NEEDS_FCNTL
def test_interrupted_probe_leaves_a_replacement_alone(
    tmp_path: Path, mocker: MockerFixture
) -> None:  # pragma: needs fcntl
    mocker.patch.object(os, "O_TMPFILE", 0, create=True)
    replacement = tmp_path / "replacement"
    replacement.write_bytes(b"peer")
    real_open = os.open
    probes: list[str] = []

    def replace_after_open(path: str, flags: int, mode: int) -> int:
        fd = real_open(path, flags, mode)
        replacement.replace(path)
        probes.append(path)
        return fd

    mocker.patch("os.open", autospec=True, side_effect=replace_after_open)
    mocker.patch("filelock._native._unix.fcntl.flock", autospec=True, side_effect=KeyboardInterrupt)
    with pytest.raises(KeyboardInterrupt):
        UnixFileLock(tmp_path / "test.lock").acquire(timeout=0)
    (probe,) = tmp_path.iterdir()
    assert (str(probe), probe.read_bytes()) == (probes[0], b"peer")


@NEEDS_FCNTL
@NEEDS_STALE_RECLAIM
def test_interrupted_unix_create_marks_fallback_placeholder(
    tmp_path: Path, mocker: MockerFixture
) -> None:  # pragma: needs fcntl
    mocker.patch("filelock._native._unix.fcntl.flock", side_effect=OSError(ENOSYS, "no flock"), autospec=True)
    lock_path = tmp_path / "test.lock"
    interrupt_unix_create(UnixFileLock(lock_path, mode=0o600))
    assert lock_path.read_bytes().startswith(b"filelock/break\n")


@NEEDS_FCNTL
def test_interrupted_unix_create_honors_disabled_fallback(
    tmp_path: Path, mocker: MockerFixture
) -> None:  # pragma: needs fcntl
    mocker.patch("filelock._native._unix.fcntl.flock", side_effect=OSError(ENOSYS, "no flock"), autospec=True)
    lock_path = tmp_path / "test.lock"
    interrupt_unix_create(UnixFileLock(lock_path, fallback_to_soft=False))
    assert lock_path.exists()


@NEEDS_FCNTL
@pytest.mark.parametrize(
    "prepare", [pytest.param(lambda _: None, id="new"), pytest.param(lambda p: p.touch(), id="old")]
)
def test_oserror_after_open_does_not_retry(  # pragma: needs fcntl
    tmp_path: Path, prepare: Callable[[Path], None]
) -> None:
    lock_path = tmp_path / "test.lock"
    prepare(lock_path)
    lock = UnixFileLock(lock_path, mode=0o600)
    interrupt_unix_create(lock, PermissionError)
    with lock.acquire(timeout=0):
        assert lock.is_locked


@NEEDS_FCNTL
@pytest.mark.usefixtures("write_denied_lock_file")
def test_oserror_after_read_only_open_propagates(tmp_path: Path) -> None:  # pragma: needs fcntl
    (lock_path := tmp_path / "test.lock").touch()
    interrupt_unix_create(UnixFileLock(lock_path), BlockingIOError)
    assert lock_path.read_bytes() == b""


@NEEDS_FCNTL
def test_fchmod_enosys_is_not_a_missing_flock(tmp_path: Path, mocker: MockerFixture) -> None:  # pragma: needs fcntl
    mocker.patch("os.fchmod", side_effect=OSError(ENOSYS, "Function not implemented"), autospec=True)
    lock = UnixFileLock(tmp_path / "test.lock", mode=0o644)

    with warnings.catch_warnings(record=True) as recorded:
        warnings.simplefilter("always")
        with pytest.raises(OSError, match="Function not implemented"):
            lock.acquire()

    assert not recorded
    assert not isinstance(lock, SoftFileLock)


@NEEDS_FCNTL
@NEEDS_STALE_RECLAIM
@pytest.mark.filterwarnings("ignore:flock not supported on this filesystem:UserWarning")
@pytest.mark.usefixtures("unsupported_flock")
def test_interrupted_publication_keeps_replacement(
    tmp_path: Path, mocker: MockerFixture
) -> None:  # pragma: needs fcntl
    lock_path = tmp_path / "test.lock"
    replacement = tmp_path / "replacement"
    replacement.write_text("other record", encoding="utf-8")

    def refuse_write(_fd: int, _data: bytes) -> int:
        replacement.replace(lock_path)
        raise OSError(EIO, "write failed")

    mocker.patch("os.write", autospec=True, side_effect=refuse_write)
    with pytest.raises(OSError, match="write failed"):
        UnixFileLock(lock_path).acquire(timeout=0)
    assert lock_path.read_text(encoding="utf-8") == "other record"


@NEEDS_FCNTL
@NEEDS_STALE_RECLAIM
@pytest.mark.usefixtures("unsupported_flock")
def test_interrupted_open_preserves_marker_when_exchange_fails(  # pragma: needs fcntl
    tmp_path: Path, mocker: MockerFixture
) -> None:
    mocker.patch("filelock._native._unix.exchange_names", autospec=True, return_value=False)
    lock_path = tmp_path / "test.lock"
    interrupt_unix_create(UnixFileLock(lock_path, mode=0o600))
    assert (list(tmp_path.iterdir()), lock_path.read_bytes()) == ([lock_path], b"")


@NEEDS_FCNTL
@NEEDS_STALE_RECLAIM
@pytest.mark.usefixtures("unsupported_flock")
def test_interrupted_exchange_keeps_the_recovery_marker(  # pragma: needs fcntl
    tmp_path: Path, mocker: MockerFixture
) -> None:
    def exchange_then_interrupt(source: str, target: str) -> bool:
        assert exchange_names(source, target)
        raise KeyboardInterrupt

    mocker.patch("filelock._native._unix.exchange_names", side_effect=exchange_then_interrupt, autospec=True)
    lock_path = tmp_path / "test.lock"
    interrupt_unix_create(UnixFileLock(lock_path, mode=0o600))
    assert (list(tmp_path.iterdir()), lock_path.read_bytes()) == (
        [lock_path],
        f"filelock/break\n{os.getpid()}\n".encode(),
    )


@NEEDS_FCNTL
@NEEDS_STALE_RECLAIM
@pytest.mark.usefixtures("unsupported_flock")
def test_interrupted_open_keeps_existing_private_name(  # pragma: needs fcntl
    tmp_path: Path, mocker: MockerFixture
) -> None:
    mocker.patch("secrets.token_hex", autospec=True, return_value="fixed")
    lock_path = tmp_path / "test.lock"
    other = tmp_path / f"test.lock.break.{os.getpid()}.fixed"
    other.write_bytes(b"other record")
    interrupt_unix_create(UnixFileLock(lock_path, mode=0o600))
    assert (other.read_bytes(), lock_path.read_bytes()) == (b"other record", b"")


@NEEDS_FCNTL
@pytest.mark.usefixtures("group_writable_umask")
@pytest.mark.filterwarnings("ignore:flock not supported on this filesystem:UserWarning")
@pytest.mark.parametrize("exchange", [pytest.param(False, id="unavailable"), pytest.param(True, id="refused")])
def test_writable_target_requires_exchange_when_its_flock_differs(  # pragma: needs fcntl
    tmp_path: Path, mocker: MockerFixture, *, exchange: bool
) -> None:
    mocker.patch("filelock._native._unix.fcntl.flock", autospec=True, side_effect=[None, OSError(ENOSYS, "no flock")])
    mocker.patch("filelock._native._unix.exchange_supported", autospec=True, return_value=exchange)
    mocker.patch("filelock._native._unix.exchange_names", autospec=True, return_value=False)
    mocker.patch("filelock._soft._exclusive._recovery.exchange_names", autospec=True, return_value=False)
    lock = UnixFileLock(tmp_path / "test.lock", mode=0o666)
    with pytest.raises(PermissionError, match="soft fallback needs a private marker"):
        lock.acquire(timeout=0)
    assert not lock.is_locked


@NEEDS_FCNTL
@pytest.mark.parametrize("operation", [pytest.param("os.open", id="open"), pytest.param("fcntl.flock", id="flock")])
def test_private_probe_propagates_handler_timeout(  # pragma: needs fcntl
    tmp_path: Path, mocker: MockerFixture, operation: str
) -> None:
    mocker.patch(f"filelock._native._unix.{operation}", autospec=True, side_effect=TimeoutError("deadline"))
    with pytest.raises(TimeoutError, match="deadline"):
        UnixFileLock(tmp_path / "test.lock").acquire(timeout=0)
    assert list(tmp_path.iterdir()) == []


@NEEDS_FCNTL
@pytest.mark.usefixtures("group_writable_umask")
@pytest.mark.parametrize(
    ("make_lock", "held_mode"),
    [
        pytest.param(lambda p: UnixFileLock(p, mode=0o666), 0o666, id="explicit"),
        pytest.param(UnixFileLock, 0o644, id="umask"),
    ],
)
def test_native_file_works_when_directory_denies_a_probe(  # pragma: needs fcntl
    tmp_path: Path, make_lock: Callable[[Path], UnixFileLock], held_mode: int, restricted_creation_modes: list[int]
) -> None:
    lock_path = tmp_path / "test.lock"
    with make_lock(lock_path) as lock:
        assert (lock.is_locked, restricted_creation_modes, stat.S_IMODE(lock_path.stat().st_mode)) == (
            True,
            [0o644],
            held_mode,
        )
    assert list(tmp_path.iterdir()) == [lock_path]


@pytest.fixture
def restricted_creation_modes(tmp_path: Path, mocker: MockerFixture) -> list[int]:  # pragma: needs fcntl
    real_open = os.open
    modes: list[int] = []

    def deny_probe(path: str, flags: int, mode: int = 0o777, *, dir_fd: int | None = None) -> int:
        if flags & getattr(os, "O_TMPFILE", 0) or os.fspath(path).startswith(str(tmp_path / ".filelock-probe-")):
            raise PermissionError(EACCES, "probe denied", path)
        fd = real_open(path, flags, mode, dir_fd=dir_fd)
        if flags & os.O_CREAT and path == str(tmp_path / "test.lock"):
            modes.append(stat.S_IMODE(os.fstat(fd).st_mode))
        return fd

    mocker.patch("os.open", autospec=True, side_effect=deny_probe)
    return modes


@NEEDS_FCNTL
@pytest.mark.usefixtures("unsupported_flock", "group_writable_umask")
@pytest.mark.parametrize(
    "make_lock",
    [pytest.param(lambda p: UnixFileLock(p, mode=0o666), id="explicit"), pytest.param(UnixFileLock, id="umask")],
)
def test_target_rejects_flock_after_a_denied_probe(  # pragma: needs fcntl
    tmp_path: Path,
    make_lock: Callable[[Path], UnixFileLock],
    restricted_creation_modes: list[int],
    mocker: MockerFixture,
) -> None:
    assert sys.platform != "win32"
    mocker.patch("filelock._native._unix.exchange_supported", autospec=True, return_value=False)
    lock_path = tmp_path / "test.lock"
    with pytest.warns(UserWarning, match="falling back"), make_lock(lock_path) as lock:
        assert (lock.pid, restricted_creation_modes, stat.S_IMODE(lock_path.stat().st_mode)) == (
            os.getpid(),
            [0o644],
            0o644,
        )
    assert list(tmp_path.iterdir()) == []
