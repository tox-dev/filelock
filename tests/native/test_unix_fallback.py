from __future__ import annotations

import inspect
import os
import sys
from errno import ENOSYS
from typing import TYPE_CHECKING

import pytest

from filelock import (
    BaseFileLock,
    FileLock,
    SoftFileLock,
    Timeout,
    UnixFileLock,
    WindowsFileLock,
)
from tests.capability_marks import (
    NEEDS_FCNTL,
)

if TYPE_CHECKING:
    from pathlib import Path

    from pytest_mock import MockerFixture


import subprocess  # ruff:ignore[suspicious-subprocess-import]  # a clean interpreter isolates the blocked fcntl import
import threading
from concurrent.futures import ThreadPoolExecutor
from textwrap import dedent
from typing import TYPE_CHECKING, Final

from filelock import SoftFileLockLifetimeWarning
from filelock._identity import host_name, process_start_token

if TYPE_CHECKING:
    from pathlib import Path

    from pytest_mock import MockerFixture, MockType


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
    assert sys.platform != "win32"
    lock = UnixFileLock(tmp_path / "test.lock")

    def holder_pid() -> int | None:
        with lock.acquire(timeout=0):
            return lock.pid

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

    mocker.patch("filelock._native._unix.fcntl.flock", side_effect=flock_missing, autospec=True)
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
    assert sys.platform != "win32"
    (lock_path := tmp_path / "test.lock").touch()
    lock = UnixFileLock(lock_path)

    with pytest.raises(Timeout):
        lock.acquire(timeout=0)
    lock.break_lock()

    assert not lock_path.exists()


@NEEDS_FCNTL  # pragma: needs fcntl
def test_native_break_lock_does_not_remove_file(tmp_path: Path) -> None:
    assert sys.platform != "win32"
    (lock_path := tmp_path / "test.lock").touch()

    with pytest.raises(RuntimeError, match="requires a soft fallback"):
        UnixFileLock(lock_path).break_lock()

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
    assert sys.platform != "win32"
    first, second = working_directories
    lock = UnixFileLock("test.lock")

    with lock:
        assert SoftFileLock(first / "test.lock").is_lock_held_by_us
    mocker.stop(unsupported_flock)
    monkeypatch.chdir(second)
    (second / "test.lock").touch()

    with pytest.raises(RuntimeError, match="requires a soft fallback"):
        lock.break_lock()
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
    assert sys.platform != "win32"
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

    lock.break_lock()

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
@pytest.mark.parametrize("name", ["pid", "is_lock_held_by_us"])
def test_native_path_has_no_soft_holder(tmp_path: Path, name: str) -> None:  # pragma: needs fcntl
    lock = UnixFileLock(tmp_path / "test.lock")
    with lock:
        assert getattr(lock, name, None) is None


@pytest.mark.parametrize(
    "lock_type",
    [
        pytest.param(
            WindowsFileLock,
            id="WindowsFileLock",
            marks=pytest.mark.skipif(sys.platform == "win32", reason="WindowsFileLock is the native lock here"),
        ),
        pytest.param(
            UnixFileLock,
            id="UnixFileLock",
            marks=pytest.mark.skipif(sys.platform != "win32", reason="UnixFileLock is the native lock here"),
        ),
    ],
)
def test_wrong_platform(lock_type: type[BaseFileLock], tmp_path: Path) -> None:
    assert not inspect.isabstract(UnixFileLock)
    assert not inspect.isabstract(WindowsFileLock)
    assert inspect.isabstract(BaseFileLock)

    lock = lock_type(tmp_path / "lockfile")

    with pytest.raises(NotImplementedError):
        lock.acquire()
    with pytest.raises(NotImplementedError):
        lock._release()


@NEEDS_FCNTL  # pragma: needs fcntl
@pytest.mark.filterwarnings("ignore:flock not supported on this filesystem:UserWarning")
def test_flock_not_implemented_unix(tmp_path: Path, mocker: MockerFixture) -> None:
    mocker.patch("fcntl.flock", side_effect=OSError(ENOSYS, "mock error"), autospec=True)
    lock = FileLock(tmp_path / "a.lock")
    with lock:
        assert lock.is_locked
        assert SoftFileLock(tmp_path / "a.lock").is_lock_held_by_us


@NEEDS_FCNTL  # pragma: needs fcntl
def test_fallback_to_soft_disabled_raises_enosys(tmp_path: Path, mocker: MockerFixture) -> None:
    mocker.patch("filelock._native._unix.fcntl.flock", side_effect=OSError(ENOSYS, "no flock"), autospec=True)
    lock = FileLock(str(tmp_path / "a"), fallback_to_soft=False)
    with pytest.raises(OSError, match="no flock") as info:
        lock.acquire()
    assert info.value.errno == ENOSYS
    assert not lock.is_locked
    assert type(lock).__name__ == "UnixFileLock"
    assert lock.lock_counter == 0


@NEEDS_FCNTL  # pragma: needs fcntl
def test_fallback_to_soft_default_switches_to_soft(tmp_path: Path, mocker: MockerFixture) -> None:
    mocker.patch("filelock._native._unix.fcntl.flock", side_effect=OSError(ENOSYS, "no flock"), autospec=True)
    lock = FileLock(str(tmp_path / "a"))
    with pytest.warns(UserWarning, match="falling back to SoftFileLock"):
        lock.acquire()
    try:
        assert lock.is_locked
        assert SoftFileLock(str(tmp_path / "a")).is_lock_held_by_us
    finally:
        lock.release()


@NEEDS_FCNTL  # pragma: needs fcntl
def test_fallback_disabled_recursive_reraises(tmp_path: Path, mocker: MockerFixture) -> None:
    mocker.patch("filelock._native._unix.fcntl.flock", side_effect=OSError(ENOSYS, "no flock"), autospec=True)
    lock = FileLock(str(tmp_path / "a"), fallback_to_soft=False)
    for _ in range(2):
        with pytest.raises(OSError, match="no flock"):
            lock.acquire()
        assert not lock.is_locked


@NEEDS_FCNTL  # pragma: needs fcntl
def test_singleton_rejects_different_fallback_to_soft(tmp_path: Path) -> None:
    path = str(tmp_path / "a")
    first = FileLock(path, is_singleton=True, fallback_to_soft=True)
    try:
        with pytest.raises(ValueError, match="fallback_to_soft"):
            FileLock(path, is_singleton=True, fallback_to_soft=False)
    finally:
        first.release(force=True)


@NEEDS_FCNTL  # pragma: needs fcntl
def test_unix_acquire_without_o_nofollow(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delattr(os, "O_NOFOLLOW", raising=False)
    lock = FileLock(str(tmp_path / "a"), is_singleton=False)
    lock.acquire()
    try:
        assert lock.is_locked
    finally:
        lock.release()


@NEEDS_FCNTL  # pragma: needs fcntl
def test_unix_soft_fallback_keeps_a_replaced_file(tmp_path: Path, mocker: MockerFixture) -> None:
    lock_path = tmp_path / "a"
    lock = FileLock(str(lock_path), is_singleton=False)
    target = lock.lock_file
    mocker.patch("filelock._native._unix.fcntl.flock", side_effect=OSError(ENOSYS, "no flock"), autospec=True)

    real_lstat = os.lstat

    def lstat_swapped_identity(path: str) -> os.stat_result:
        st = real_lstat(path)
        if str(path) != target:
            return st
        fields = list(st)
        fields[1] += 1  # bump st_ino so the on-disk identity no longer matches the opened fd
        return os.stat_result(fields)

    mocker.patch("filelock._native._unix.os.lstat", side_effect=lstat_swapped_identity, autospec=True)

    with pytest.raises(Timeout), pytest.warns(UserWarning, match="falling back to SoftFileLock"):
        lock.acquire(timeout=0.2)

    assert lock_path.exists()
