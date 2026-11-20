from __future__ import annotations

import os
import stat
import subprocess  # ruff:ignore[suspicious-subprocess-import]  # a clean interpreter isolates the blocked fcntl import
import sys
import threading
import warnings
from concurrent.futures import ThreadPoolExecutor
from errno import EACCES, EBADF, EIO, ENOSYS, EWOULDBLOCK
from textwrap import dedent
from typing import TYPE_CHECKING, Final, cast

import pytest

from filelock import SoftFileLock, SoftFileLockLifetimeWarning, Timeout, UnixFileLock
from filelock._identity import host_name, process_start_token
from filelock._util import exchange_names
from tests.capability_marks import NEEDS_FCNTL, NEEDS_STALE_RECLAIM

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path
    from types import BuiltinFunctionType, FrameType
    from typing import Protocol

    from pytest_mock import MockerFixture, MockType

    class BreakableLock(Protocol):
        @property
        def pid(self) -> int | None: ...

        @property
        def is_lock_held_by_us(self) -> bool: ...

        def break_lock(self) -> None: ...


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
def test_interrupted_unix_create_keeps_native_file(tmp_path: Path) -> None:  # pragma: needs fcntl
    lock_path = tmp_path / "test.lock"
    _interrupt_unix_create(UnixFileLock(lock_path))
    assert lock_path.exists()
    with UnixFileLock(lock_path, timeout=0) as lock:
        assert lock.is_locked


def _interrupt_unix_create(
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


@NEEDS_FCNTL
@NEEDS_STALE_RECLAIM
def test_interrupted_unix_create_marks_fallback_placeholder(
    tmp_path: Path, mocker: MockerFixture
) -> None:  # pragma: needs fcntl
    mocker.patch("filelock._unix.fcntl.flock", side_effect=OSError(ENOSYS, "no flock"), autospec=True)
    lock_path = tmp_path / "test.lock"
    _interrupt_unix_create(UnixFileLock(lock_path, mode=0o600))
    assert lock_path.read_bytes().startswith(b"filelock/break\n")


@NEEDS_FCNTL
def test_interrupted_unix_create_honors_disabled_fallback(
    tmp_path: Path, mocker: MockerFixture
) -> None:  # pragma: needs fcntl
    mocker.patch("filelock._unix.fcntl.flock", side_effect=OSError(ENOSYS, "no flock"), autospec=True)
    lock_path = tmp_path / "test.lock"
    _interrupt_unix_create(UnixFileLock(lock_path, fallback_to_soft=False))
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
    lock = UnixFileLock(lock_path)
    _interrupt_unix_create(lock, PermissionError)
    with lock.acquire(timeout=0):
        assert lock.is_locked


@NEEDS_FCNTL
@pytest.mark.usefixtures("write_denied_lock_file")
def test_oserror_after_read_only_open_propagates(tmp_path: Path) -> None:  # pragma: needs fcntl
    (lock_path := tmp_path / "test.lock").touch()
    _interrupt_unix_create(UnixFileLock(lock_path), BlockingIOError)
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
@pytest.mark.filterwarnings("ignore:flock not supported on this filesystem:UserWarning")
@pytest.mark.usefixtures("unsupported_flock")
def test_fallback_keeps_unix_class_and_holds_soft_marker(tmp_path: Path) -> None:  # pragma: needs fcntl
    lock = UnixFileLock(tmp_path / "test.lock")

    with lock:
        assert lock.is_locked
        assert type(lock) is UnixFileLock
        assert SoftFileLock(tmp_path / "test.lock").is_lock_held_by_us


@NEEDS_FCNTL
@pytest.mark.filterwarnings("ignore:flock not supported on this filesystem:UserWarning")
@pytest.mark.usefixtures("unsupported_flock")
@pytest.mark.parametrize(
    "expected_lines",
    [
        pytest.param(
            [str(os.getpid()), host_name(), str(process_start_token(os.getpid()))],
            id="start_token",
            marks=pytest.mark.skipif(
                process_start_token(os.getpid()) is None,
                reason="the start token is unreadable here, e.g. under a time namespace with a boottime offset",
            ),
        ),
        pytest.param(
            [str(os.getpid()), host_name()],
            id="no_start_token",
            marks=pytest.mark.skipif(
                process_start_token(os.getpid()) is not None,
                reason="the start token is readable here, so the lock writes it as a third line",
            ),
        ),
    ],
)
def test_fallback_writes_pid_and_hostname(tmp_path: Path, expected_lines: list[str]) -> None:  # pragma: needs fcntl
    lock_path = tmp_path / "test.lock"

    with UnixFileLock(lock_path):
        lines = lock_path.read_text(encoding="utf-8").splitlines()
    assert lines == expected_lines


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
def test_fallback_subsequent_acquire_skips_flock(tmp_path: Path, unsupported_flock: MockType) -> None:
    lock = UnixFileLock(tmp_path / "test.lock")

    lock.acquire()
    lock.release()
    unsupported_flock.reset_mock()

    lock.acquire()
    lock.release()
    unsupported_flock.assert_not_called()


@NEEDS_FCNTL
@pytest.mark.filterwarnings("ignore:flock not supported on this filesystem:UserWarning")  # pragma: needs fcntl
def test_fallback_reprobes_after_chdir(
    working_directories: tuple[Path, Path],
    unsupported_flock: MockType,
    mocker: MockerFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first, second = working_directories
    lock = UnixFileLock("test.lock")

    with lock:
        assert SoftFileLock(first / "test.lock").is_lock_held_by_us
    mocker.stop(unsupported_flock)
    monkeypatch.chdir(second)

    with lock, pytest.raises(Timeout):
        UnixFileLock(second / "test.lock").acquire(timeout=0)


@NEEDS_FCNTL
def test_fallback_skips_flock_in_another_thread(  # pragma: needs fcntl
    tmp_path: Path, unsupported_flock: MockType
) -> None:
    lock = UnixFileLock(tmp_path / "test.lock")

    def holder_pid() -> int | None:
        with lock.acquire(timeout=0):
            return cast("BreakableLock", lock).pid

    with pytest.warns(UserWarning, match="flock not supported"):
        assert holder_pid() == os.getpid()
    unsupported_flock.reset_mock()
    with ThreadPoolExecutor(max_workers=1) as worker:
        assert worker.submit(holder_pid).result(timeout=5) == os.getpid()
    unsupported_flock.assert_not_called()


@NEEDS_FCNTL
@pytest.mark.filterwarnings("ignore:flock not supported on this filesystem:UserWarning")  # pragma: needs fcntl
def test_concurrent_fallback_keeps_both_acquires_valid(tmp_path: Path, mocker: MockerFixture) -> None:
    lock_path = tmp_path / "test.lock"
    lock = UnixFileLock(lock_path, mode=0o600)
    in_flock, fell_back = threading.Event(), threading.Event()
    acquired: list[bool] = []

    def flock_missing(fd: int, operation: int) -> None:
        if threading.current_thread() is waiter:
            in_flock.set()
            fell_back.wait(timeout=5)
        raise OSError(ENOSYS, f"Function not implemented for {fd} with {operation}")

    def acquire_after_peer_fallback() -> None:
        with lock.acquire(timeout=1):
            acquired.append(SoftFileLock(lock_path).is_lock_held_by_us)

    mocker.patch("filelock._unix.fcntl.flock", side_effect=flock_missing, autospec=True)
    waiter = threading.Thread(target=acquire_after_peer_fallback)
    waiter.start()
    assert in_flock.wait(timeout=5)
    with pytest.raises(Timeout):
        lock.acquire(timeout=0)
    fell_back.set()
    waiter.join(timeout=5)

    assert not waiter.is_alive()
    assert acquired == [True]
    assert not lock_path.exists()


@NEEDS_FCNTL
@pytest.mark.filterwarnings("ignore:flock not supported on this filesystem:UserWarning")
@pytest.mark.usefixtures("unsupported_flock")
def test_fallback_timeout_describes_recordless_marker(tmp_path: Path) -> None:  # pragma: needs fcntl
    (lock_path := tmp_path / "test.lock").touch()

    with pytest.raises(Timeout, match="held by an owner that recorded no PID"):
        UnixFileLock(lock_path).acquire(timeout=0)


@NEEDS_FCNTL
@pytest.mark.filterwarnings("ignore:flock not supported on this filesystem:UserWarning")
@pytest.mark.usefixtures("unsupported_flock")
def test_fallback_break_lock_removes_recordless_marker(tmp_path: Path) -> None:  # pragma: needs fcntl
    (lock_path := tmp_path / "test.lock").touch()
    lock = UnixFileLock(lock_path)

    with pytest.raises(Timeout):
        lock.acquire(timeout=0)
    cast("BreakableLock", lock).break_lock()

    assert not lock_path.exists()


@NEEDS_FCNTL  # pragma: needs fcntl
def test_native_break_lock_does_not_remove_file(tmp_path: Path) -> None:
    (lock_path := tmp_path / "test.lock").touch()

    with pytest.raises(RuntimeError, match="requires a soft fallback"):
        cast("BreakableLock", UnixFileLock(lock_path)).break_lock()

    assert lock_path.exists()


@NEEDS_FCNTL
@pytest.mark.filterwarnings("ignore:flock not supported on this filesystem:UserWarning")
@pytest.mark.usefixtures("unsupported_flock")
def test_fallback_lifetime_expires_recordless_marker(tmp_path: Path) -> None:  # pragma: needs fcntl
    (lock_path := tmp_path / "test.lock").touch()
    lock = UnixFileLock(lock_path)

    with pytest.raises(Timeout):
        lock.acquire(timeout=0)
    with pytest.warns(SoftFileLockLifetimeWarning):
        lock.lifetime = 0
    with lock.acquire(timeout=0):
        assert SoftFileLock(lock_path).is_lock_held_by_us


@NEEDS_FCNTL
@pytest.mark.filterwarnings("ignore:flock not supported on this filesystem:UserWarning")  # pragma: needs fcntl
def test_fallback_recovery_does_not_apply_after_chdir(
    working_directories: tuple[Path, Path],
    unsupported_flock: MockType,
    mocker: MockerFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first, second = working_directories
    lock = UnixFileLock("test.lock")

    with lock:
        assert SoftFileLock(first / "test.lock").is_lock_held_by_us
    mocker.stop(unsupported_flock)
    monkeypatch.chdir(second)
    (second / "test.lock").touch()

    with pytest.raises(RuntimeError, match="requires a soft fallback"):
        cast("BreakableLock", lock).break_lock()
    with pytest.warns(UserWarning, match="lifetime is ignored for UnixFileLock"):
        lock.lifetime = 0

    assert (second / "test.lock").exists()
    assert lock.lifetime is None


@NEEDS_FCNTL
@pytest.mark.filterwarnings("ignore:flock not supported on this filesystem:UserWarning")  # pragma: needs fcntl
@pytest.mark.usefixtures("unsupported_flock")
def test_fallback_recovery_remembers_earlier_path(
    working_directories: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    first, second = working_directories
    (first / "test.lock").touch()
    lock = UnixFileLock("test.lock")

    with pytest.raises(Timeout):
        lock.acquire(timeout=0)
    monkeypatch.chdir(second)
    (second / "test.lock").touch()
    with pytest.raises(Timeout):
        lock.acquire(timeout=0)
    monkeypatch.chdir(first)

    cast("BreakableLock", lock).break_lock()

    assert not (first / "test.lock").exists()
    (first / "test.lock").touch()
    with pytest.warns(SoftFileLockLifetimeWarning):
        lock.lifetime = 0
    with lock.acquire(timeout=0):
        assert SoftFileLock(first / "test.lock").is_lock_held_by_us


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
def test_fallback_publishes_in_the_file_it_created(tmp_path: Path) -> None:  # pragma: needs fcntl
    lock = UnixFileLock(tmp_path / "test.lock")

    lock.acquire(timeout=0)
    assert lock.is_locked
    marker = cast("BreakableLock", lock)
    assert (marker.pid, marker.is_lock_held_by_us) == (os.getpid(), True)
    assert (tmp_path / "test.lock").read_bytes().startswith(f"{os.getpid()}\n".encode())
    lock.release()


@NEEDS_FCNTL
@pytest.mark.filterwarnings("ignore:flock not supported on this filesystem:UserWarning")
@pytest.mark.usefixtures("unsupported_flock")
def test_fallback_write_error_leaves_marker(tmp_path: Path, mocker: MockerFixture) -> None:  # pragma: needs fcntl
    lock_path = tmp_path / "test.lock"
    mocker.patch("os.write", side_effect=OSError(EIO, "write failed"), autospec=True)

    with pytest.raises(OSError, match="write failed"):
        UnixFileLock(lock_path).acquire()

    assert lock_path.read_bytes() == b""
    with pytest.raises(Timeout):
        SoftFileLock(lock_path).acquire(timeout=0)


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
    mocker.patch("filelock._unix.exchange_names", autospec=True, return_value=False)
    lock_path = tmp_path / "test.lock"
    _interrupt_unix_create(UnixFileLock(lock_path, mode=0o600))
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

    mocker.patch("filelock._unix.exchange_names", side_effect=exchange_then_interrupt, autospec=True)
    lock_path = tmp_path / "test.lock"
    _interrupt_unix_create(UnixFileLock(lock_path, mode=0o600))
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
    _interrupt_unix_create(UnixFileLock(lock_path, mode=0o600))
    assert (other.read_bytes(), lock_path.read_bytes()) == (b"other record", b"")


@NEEDS_FCNTL
@pytest.mark.filterwarnings("ignore:flock not supported on this filesystem:UserWarning")
@pytest.mark.usefixtures("unsupported_flock")
@NEEDS_STALE_RECLAIM
@pytest.mark.parametrize(
    ("make_lock", "umask"),
    [
        pytest.param(lambda path: UnixFileLock(path, mode=0o666), 0o022, id="explicit"),
        pytest.param(UnixFileLock, 0, id="umask"),
    ],
)
def test_fallback_creates_marker_with_restricted_writes(  # pragma: needs fcntl
    tmp_path: Path, make_lock: Callable[[Path], UnixFileLock], umask: int
) -> None:
    lock_path = tmp_path / "test.lock"
    previous_umask = os.umask(umask)
    try:
        lock = make_lock(lock_path)
        with lock:
            assert (cast("BreakableLock", lock).pid, stat.S_IMODE(lock_path.stat().st_mode)) == (os.getpid(), 0o644)
    finally:
        os.umask(previous_umask)


@NEEDS_FCNTL
@pytest.mark.usefixtures("unsupported_flock")
def test_fallback_restricts_writable_marker_without_exchange(  # pragma: needs fcntl
    tmp_path: Path, mocker: MockerFixture
) -> None:
    mocker.patch("filelock._unix.exchange_supported", autospec=True, return_value=False)
    lock_path = tmp_path / "test.lock"
    with pytest.warns(UserWarning, match="falling back"), UnixFileLock(lock_path, mode=0o666) as lock:
        assert (cast("BreakableLock", lock).pid, stat.S_IMODE(lock_path.stat().st_mode)) == (os.getpid(), 0o644)
    assert list(tmp_path.iterdir()) == []


@NEEDS_FCNTL
@NEEDS_STALE_RECLAIM
@pytest.mark.filterwarnings("ignore:flock not supported on this filesystem:UserWarning")
def test_fallback_closes_the_displaced_native_descriptor(  # pragma: needs fcntl
    tmp_path: Path, mocker: MockerFixture
) -> None:
    # A mount can report different flock support for the private probe and the public file.
    mocker.patch("filelock._unix._flock_is_missing", autospec=True, return_value=False)
    flock = mocker.patch("filelock._unix.lock_fd_nonblocking", autospec=True, side_effect=OSError(ENOSYS, "no flock"))

    with UnixFileLock(tmp_path / "test.lock", mode=0o666) as lock:
        assert cast("BreakableLock", lock).is_lock_held_by_us
        with pytest.raises(OSError, match=os.strerror(EBADF)) as closed:
            os.fstat(flock.call_args.args[0])
        assert closed.value.errno == EBADF
    assert list(tmp_path.iterdir()) == []


@NEEDS_FCNTL
@pytest.mark.parametrize(
    "operation", [pytest.param("tempfile.TemporaryFile", id="open"), pytest.param("lock_fd_nonblocking", id="flock")]
)
def test_private_probe_propagates_handler_timeout(  # pragma: needs fcntl
    tmp_path: Path, mocker: MockerFixture, operation: str
) -> None:
    mocker.patch(f"filelock._unix.{operation}", autospec=True, side_effect=TimeoutError("deadline"))
    with pytest.raises(TimeoutError, match="deadline"):
        UnixFileLock(tmp_path / "test.lock").acquire(timeout=0)
    assert list(tmp_path.iterdir()) == []


@NEEDS_FCNTL
def test_native_file_works_when_directory_denies_a_probe(  # pragma: needs fcntl
    tmp_path: Path, mocker: MockerFixture
) -> None:
    mocker.patch(
        "filelock._unix.tempfile.TemporaryFile", autospec=True, side_effect=PermissionError(EACCES, "probe denied")
    )
    lock_path = tmp_path / "test.lock"
    with UnixFileLock(lock_path, mode=0o666) as lock:
        assert (lock.is_locked, stat.S_IMODE(lock_path.stat().st_mode)) == (True, 0o666)


@NEEDS_FCNTL
@pytest.mark.filterwarnings("ignore:flock not supported on this filesystem:UserWarning")
@pytest.mark.usefixtures("unsupported_flock")
@pytest.mark.parametrize("platform_exchange", [pytest.param(False, id="unavailable"), pytest.param(True, id="refused")])
def test_target_rejects_flock_after_a_denied_probe(  # pragma: needs fcntl
    tmp_path: Path, mocker: MockerFixture, platform_exchange: bool
) -> None:
    mocker.patch(
        "filelock._unix.tempfile.TemporaryFile", autospec=True, side_effect=PermissionError(EACCES, "probe denied")
    )
    mocker.patch("filelock._unix.exchange_supported", autospec=True, return_value=platform_exchange)
    mocker.patch("filelock._unix.exchange_names", autospec=True, return_value=False)
    mocker.patch("filelock._soft.exchange_names", autospec=True, return_value=False)
    lock_path = tmp_path / "test.lock"
    with pytest.raises(PermissionError, match="soft fallback needs a private marker"):
        UnixFileLock(lock_path, mode=0o666).acquire(timeout=0)
    assert (list(tmp_path.iterdir()), lock_path.read_bytes()) == ([lock_path], b"")


@NEEDS_FCNTL
@pytest.mark.parametrize("name", ["pid", "is_lock_held_by_us"])
def test_native_path_has_no_soft_holder(tmp_path: Path, name: str) -> None:  # pragma: needs fcntl
    lock = UnixFileLock(tmp_path / "test.lock")
    with lock:
        assert getattr(lock, name, None) is None


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
def test_fallback_keeps_the_created_marker_without_unlinking(  # pragma: needs fcntl
    tmp_path: Path, mocker: MockerFixture
) -> None:
    lock_path = tmp_path / "test.lock"
    peer = SoftFileLock(lock_path)
    unlink = mocker.spy(os, "unlink")
    mocker.patch("filelock._unix.fcntl.flock", side_effect=OSError(ENOSYS, "no flock"), autospec=True)
    lock = UnixFileLock(lock_path)
    try:
        lock.acquire(timeout=1)
        assert unlink.call_count == 0
        with pytest.raises(Timeout):
            peer.acquire(timeout=0)
    finally:
        mocker.stopall()
        lock.release()
        peer.release()


@NEEDS_FCNTL
@pytest.mark.filterwarnings("ignore:flock not supported on this filesystem:UserWarning")
@pytest.mark.usefixtures("unsupported_flock")
def test_fallback_retries_if_peer_replaces_unwritten_marker(  # pragma: needs fcntl
    tmp_path: Path, mocker: MockerFixture
) -> None:
    lock_path = tmp_path / "test.lock"
    peer = SoftFileLock(lock_path)
    write = os.write
    replaced: list[bool] = []

    def write_after_peer_replaces_marker(fd: int, data: bytes) -> int:
        if not replaced:
            replaced.append(True)
            lock_path.unlink()
            peer.acquire(blocking=False)
        return write(fd, data)

    mocker.patch("os.write", side_effect=write_after_peer_replaces_marker, autospec=True)
    lock = UnixFileLock(lock_path)
    try:
        with pytest.raises(Timeout):
            lock.acquire(timeout=0)
        assert peer.is_locked
        assert not lock.is_locked
    finally:
        mocker.stopall()
        lock.release()
        peer.release()


@NEEDS_FCNTL
@pytest.mark.filterwarnings("ignore:flock not supported on this filesystem:UserWarning")
@pytest.mark.usefixtures("unsupported_flock")
def test_fallback_retries_if_created_marker_disappears(  # pragma: needs fcntl
    tmp_path: Path, mocker: MockerFixture
) -> None:
    lock_path = tmp_path / "test.lock"
    lstat = os.lstat
    vanished: list[bool] = []

    def disappear_before_publish(path: str, *, dir_fd: int | None = None) -> os.stat_result:
        marker = lstat(path, dir_fd=dir_fd)
        if os.fspath(path) == os.fspath(lock_path) and not vanished:
            vanished.append(True)
            lock_path.unlink()
            raise FileNotFoundError(path)
        return marker

    mocker.patch("os.lstat", side_effect=disappear_before_publish, autospec=True)
    lock = UnixFileLock(lock_path)
    try:
        lock.acquire(timeout=0)
        assert (vanished, lock.is_locked) == ([True], True)
        assert lock_path.read_bytes().startswith(f"{os.getpid()}\n".encode())
    finally:
        mocker.stopall()
        lock.release()


@NEEDS_FCNTL
@pytest.mark.filterwarnings("ignore:flock not supported on this filesystem:UserWarning")
@pytest.mark.usefixtures("unsupported_flock")
def test_fallback_lstat_error_closes_pending_descriptor(
    tmp_path: Path, mocker: MockerFixture
) -> None:  # pragma: needs fcntl
    lock_path = tmp_path / "test.lock"
    mocker.patch("os.lstat", side_effect=OSError(EIO, "lstat failed"), autospec=True)
    lock = UnixFileLock(lock_path)

    with pytest.raises(OSError, match="lstat failed"):
        lock.acquire(timeout=0)

    assert not lock.is_locked
    assert lock_path.read_bytes() == b""


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
def test_denied_lock_file_is_reopened_in_the_acquisition_directory(  # pragma: needs fcntl
    working_directories: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch, mocker: MockerFixture
) -> None:
    # Resolved against the working directory of the moment, a chdir between the refused O_RDWR open and the read-only
    # reopen probes a file in another directory, or none.
    first, second = working_directories
    (first / "test.lock").touch()
    real_open = os.open
    read_only_opens: list[str] = []

    def deny_write_then_chdir(path: str, flags: int, mode: int = 0o777, *, dir_fd: int | None = None) -> int:
        if not flags & os.O_CREAT and "test.lock" in path:
            if flags & os.O_RDWR:
                monkeypatch.chdir(second)
                raise PermissionError(EACCES, "Permission denied", path)
            read_only_opens.append(path)
        return real_open(path, flags, mode, dir_fd=dir_fd)

    mocker.patch("os.open", side_effect=deny_write_then_chdir, autospec=True)
    with pytest.raises(PermissionError):
        UnixFileLock("test.lock", mode=0o600).acquire(timeout=0)

    assert read_only_opens == [str(first.resolve() / "test.lock")]


@NEEDS_FCNTL
@pytest.mark.parametrize("write_denied_lock_file", ["leased"], indirect=True)
@pytest.mark.usefixtures("write_denied_lock_file")
def test_leased_lock_file_it_cannot_write_is_contention_where_flock_works(
    tmp_path: Path,
) -> None:  # pragma: needs fcntl
    (lock_path := tmp_path / "test.lock").touch()

    with pytest.raises(Timeout):
        UnixFileLock(lock_path).acquire(timeout=0)


@NEEDS_FCNTL
@pytest.mark.filterwarnings("ignore:flock not supported on this filesystem:UserWarning")
@pytest.mark.parametrize("write_denied_lock_file", ["leased"], indirect=True)
@pytest.mark.usefixtures("unsupported_flock", "write_denied_lock_file")
def test_fallback_treats_a_leased_marker_it_cannot_write_as_contention(tmp_path: Path) -> None:  # pragma: needs fcntl
    (lock_path := tmp_path / "test.lock").touch()

    with pytest.raises(Timeout):
        UnixFileLock(lock_path).acquire(timeout=0)


@NEEDS_FCNTL
@pytest.mark.parametrize(
    "write_denied_lock_file", [pytest.param("unlink", id="unlinked-under-the-denial")], indirect=True
)
@pytest.mark.usefixtures("write_denied_lock_file")
def test_lock_file_unlinked_under_a_denied_open_is_retried(tmp_path: Path) -> None:  # pragma: needs fcntl
    (lock_path := tmp_path / "test.lock").touch()

    with (lock := UnixFileLock(lock_path)).acquire(timeout=1):
        assert lock.is_locked


@pytest.fixture
def write_denied_lock_file(
    request: pytest.FixtureRequest, tmp_path: Path, mocker: MockerFixture
) -> None:  # pragma: needs fcntl
    # Stand in for a marker another account owns: an O_RDWR open is refused, a read works unless the file is leased.
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

    mocker.patch("filelock._unix.os.close", side_effect=_close_eio, autospec=True)
    lock.release()
    assert not lock.is_locked


@NEEDS_FCNTL  # pragma: needs fcntl
def test_acquire_flock_error_clears_pending_descriptor(tmp_path: Path, mocker: MockerFixture) -> None:
    lock = UnixFileLock(tmp_path / "test.lock")
    failed_flock = mocker.patch(
        "filelock._unix.fcntl.flock", side_effect=OSError(EIO, "Input/output error"), autospec=True
    )

    with pytest.raises(OSError, match="Input/output error"):
        lock.acquire(timeout=0)
    mocker.stop(failed_flock)
    assert not lock.is_locked

    lock.acquire(timeout=0)
    assert lock.is_locked
    lock.release()
    assert not lock.is_locked


@pytest.fixture
def unsupported_flock(mocker: MockerFixture) -> MockType:  # pragma: needs fcntl
    return mocker.patch(
        "filelock._unix.fcntl.flock", side_effect=OSError(ENOSYS, "Function not implemented"), autospec=True
    )
