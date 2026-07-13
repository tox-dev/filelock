from __future__ import annotations

import inspect
import logging
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from errno import EAGAIN, EINTR, EIO, ENOSPC, ENOSYS, EWOULDBLOCK
from inspect import getframeinfo, stack
from pathlib import Path, PurePath
from stat import S_IMODE, S_IWGRP, S_IWOTH, S_IWUSR, filemode
from types import TracebackType
from typing import TYPE_CHECKING, Any, Final
from uuid import uuid4
from weakref import WeakValueDictionary

import pytest

from filelock import BaseFileLock, ContextErrorPolicy, FileLock, SoftFileLock, Timeout, UnixFileLock, WindowsFileLock

if sys.version_info >= (3, 11):  # pragma: no cover (py311+)
    from builtins import BaseExceptionGroup, ExceptionGroup
else:  # pragma: no cover (<py311)
    from exceptiongroup import BaseExceptionGroup, ExceptionGroup

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from contextlib import AbstractContextManager

    from pytest_mock import MockerFixture


@pytest.mark.parametrize("lock_type", [FileLock, SoftFileLock])
@pytest.mark.parametrize("path_type", [str, PurePath, Path])
@pytest.mark.parametrize("filename", ["a", "new/b", "new2/new3/c"])
def test_simple(
    lock_type: type[BaseFileLock],
    path_type: type[str | Path],
    filename: str,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)

    lock_path = tmp_path / filename
    lock = lock_type(path_type(lock_path))
    with lock as locked:
        assert lock.is_locked
        assert lock is locked
    assert not lock.is_locked

    assert caplog.messages == [
        f"Attempting to acquire lock {id(lock)} on {lock_path}",
        f"Lock {id(lock)} acquired on {lock_path}",
        f"Attempting to release lock {id(lock)} on {lock_path}",
        f"Lock {id(lock)} released on {lock_path}",
    ]
    assert [r.levelno for r in caplog.records] == [logging.DEBUG, logging.DEBUG, logging.DEBUG, logging.DEBUG]
    assert [r.name for r in caplog.records] == ["filelock", "filelock", "filelock", "filelock"]
    assert logging.getLogger("filelock").level == logging.NOTSET


@contextmanager
def make_ro(path: Path) -> Iterator[None]:
    write = S_IWUSR | S_IWGRP | S_IWOTH
    path.chmod(path.stat().st_mode & ~write)
    try:
        yield
    finally:
        path.chmod(path.stat().st_mode | write)


@pytest.fixture
def tmp_path_ro(tmp_path: Path) -> Iterator[Path]:
    with make_ro(tmp_path):
        yield tmp_path


@pytest.mark.parametrize("lock_type", [FileLock, SoftFileLock])
@pytest.mark.skipif(sys.platform == "win32", reason="Windows does not have read only folders")
@pytest.mark.skipif(
    sys.platform != "win32" and os.geteuid() == 0,
    reason="Cannot make a read only file (that the current user: root can't read)",
)
def test_ro_folder(lock_type: type[BaseFileLock], tmp_path_ro: Path) -> None:
    lock = lock_type(str(tmp_path_ro / "a"))
    with pytest.raises(PermissionError, match="Permission denied"):
        lock.acquire()


@pytest.fixture
def tmp_file_ro(tmp_path: Path) -> Iterator[Path]:
    filename = tmp_path / "a"
    filename.write_text("")
    with make_ro(filename):
        yield filename


@pytest.mark.parametrize("lock_type", [FileLock, SoftFileLock])
@pytest.mark.skipif(
    sys.platform != "win32" and os.geteuid() == 0,
    reason="Cannot make a read only file (that the current user: root can't read)",
)
def test_ro_file(lock_type: type[BaseFileLock], tmp_file_ro: Path) -> None:
    lock = lock_type(str(tmp_file_ro))
    with pytest.raises(PermissionError, match="Permission denied"):
        lock.acquire()


_WINDOWS_ONLY: Final[pytest.MarkDecorator] = pytest.mark.skipif(sys.platform != "win32", reason="Windows only")
_UNIX_FLOCK_ONLY: Final[pytest.MarkDecorator] = pytest.mark.skipif(
    sys.platform == "win32", reason="native flock semantics are Unix-only"
)


@pytest.mark.parametrize("lock_type", [FileLock, SoftFileLock])
@pytest.mark.parametrize(
    ("expected_error", "match", "bad_lock_file"),
    [
        # WindowsFileLock raises the real Win32 error the NTSTATUS maps to, so accept its wording alongside os.open's.
        pytest.param(
            OSError,
            "No such file or directory:|cannot find the (path|file)|syntax is incorrect|Access is denied",
            "",
            id="blank_filename",
        ),
        pytest.param(ValueError, "embedded null (byte|character)", "\0", id="null_byte"),
        # Should be PermissionError on Windows
        (
            pytest.param(PermissionError, "Permission denied:", ".", id="current_directory")
            if sys.platform == "win32"
            # Should be IsADirectoryError on MacOS and Linux
            else (
                pytest.param(IsADirectoryError, "Is a directory", ".", id="current_directory")
                if sys.platform in {"darwin", "linux"}
                # Should be some type of OSError at least on other operating systems
                else pytest.param(OSError, None, ".", id="current_directory")
            )
        ),
    ]
    + [
        pytest.param(OSError, "Invalid argument|syntax is incorrect", i, id=f"invalid_{i}", marks=_WINDOWS_ONLY)
        for i in '<>:"|?*\a'
    ]
    + [
        pytest.param(PermissionError, "Permission denied:", i, id=f"permission_{i}", marks=_WINDOWS_ONLY) for i in "/\\"
    ],
)
@pytest.mark.timeout(5)  # timeout in case of infinite loop
def test_bad_lock_file(
    lock_type: type[BaseFileLock],
    expected_error: type[Exception],
    match: str,
    bad_lock_file: str,
) -> None:
    lock = lock_type(bad_lock_file)

    with pytest.raises(expected_error, match=match):
        lock.acquire()


@pytest.mark.parametrize("lock_type", [FileLock, SoftFileLock])
def test_nested_context_manager(lock_type: type[BaseFileLock], tmp_path: Path) -> None:
    lock_path = tmp_path / "a"
    lock = lock_type(str(lock_path))

    with lock as lock_1:
        assert lock.is_locked
        assert lock is lock_1

        with lock as lock_2:
            assert lock.is_locked
            assert lock is lock_2

            with lock as lock_3:
                assert lock.is_locked
                assert lock is lock_3

            assert lock.is_locked
        assert lock.is_locked
    assert not lock.is_locked


@pytest.mark.parametrize("lock_type", [FileLock, SoftFileLock])
def test_nested_acquire(lock_type: type[BaseFileLock], tmp_path: Path) -> None:
    lock_path = tmp_path / "a"
    lock = lock_type(str(lock_path))

    with lock.acquire() as lock_1:
        assert lock.is_locked
        assert lock is lock_1

        with lock.acquire() as lock_2:
            assert lock.is_locked
            assert lock is lock_2

            with lock.acquire() as lock_3:
                assert lock.is_locked
                assert lock is lock_3

            assert lock.is_locked
        assert lock.is_locked
    assert not lock.is_locked


@pytest.mark.parametrize("lock_type", [FileLock, SoftFileLock])
def test_nested_forced_release(lock_type: type[BaseFileLock], tmp_path: Path) -> None:
    lock_path = tmp_path / "a"
    lock = lock_type(str(lock_path))

    with lock:
        assert lock.is_locked

        lock.acquire()
        assert lock.is_locked

        lock.release(force=True)
        assert not lock.is_locked
    assert not lock.is_locked


@pytest.mark.parametrize("lock_type", [FileLock, SoftFileLock])
def test_nested_contruct(lock_type: type[BaseFileLock], tmp_path: Path) -> None:
    lock_path = tmp_path / "a"

    with lock_type(str(lock_path), is_singleton=True, timeout=2) as lock_1:
        assert lock_1.is_locked

        with lock_type(str(lock_path), is_singleton=True, timeout=2) as lock_2:
            assert lock_2 is lock_1
            assert lock_2.is_locked

        assert lock_1.is_locked

    assert not lock_1.is_locked


_ExcInfoType = tuple[type[BaseException], BaseException, TracebackType] | tuple[None, None, None]


class ExThread(threading.Thread):
    def __init__(self, target: Callable[[], None], name: str) -> None:
        super().__init__(target=target, name=name)
        self.ex: _ExcInfoType | None = None

    def run(self) -> None:
        try:
            super().run()
        except Exception:  # pragma: no cover
            self.ex = sys.exc_info()  # pragma: no cover

    def join(self, timeout: float | None = None) -> None:
        super().join(timeout=timeout)
        if self.ex is not None:
            raise RuntimeError from self.ex[1]  # pragma: no cover


@pytest.mark.parametrize("lock_type", [FileLock, SoftFileLock])
def test_threaded_shared_lock_obj(lock_type: type[BaseFileLock], tmp_path: Path) -> None:
    if sys.platform == "win32" and lock_type.__name__ == "SoftFileLock":
        pytest.skip(
            "SoftFileLock uses file-existence locking — on Windows, unlink can silently fail under heavy "
            "thread contention (EACCES from antivirus/indexer), orphaning the lock file with no recovery path"
        )

    lock_path = tmp_path / "a"
    lock = lock_type(str(lock_path))

    def thread_work() -> None:
        for _ in range(100):
            with lock:
                assert lock.is_locked

    threads = [ExThread(target=thread_work, name=f"t{i}") for i in range(100)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert not lock.is_locked


@pytest.mark.parametrize("lock_type", [FileLock, SoftFileLock])
def test_threaded_lock_different_lock_obj(lock_type: type[BaseFileLock], tmp_path: Path) -> None:
    if sys.platform == "win32" and (hasattr(sys, "pypy_version_info") or lock_type.__name__ == "SoftFileLock"):
        pytest.skip("SoftFileLock on Windows has race conditions under heavy threading")

    def t_1() -> None:
        for _ in range(1000):
            with lock_1:
                assert lock_1.is_locked
                assert not lock_2.is_locked

    def t_2() -> None:
        for _ in range(1000):
            with lock_2:
                assert not lock_1.is_locked
                assert lock_2.is_locked

    lock_path = tmp_path / "a"
    lock_1, lock_2 = lock_type(str(lock_path)), lock_type(str(lock_path))
    threads = [(ExThread(t_1, f"t1_{i}"), ExThread(t_2, f"t2_{i}")) for i in range(10)]

    for thread_1, thread_2 in threads:
        thread_1.start()
        thread_2.start()
    for thread_1, thread_2 in threads:
        thread_1.join()
        thread_2.join()

    assert not lock_1.is_locked
    assert not lock_2.is_locked


@pytest.mark.parametrize("lock_type", [FileLock, SoftFileLock])
def test_timeout(lock_type: type[BaseFileLock], tmp_path: Path) -> None:
    lock_path = tmp_path / "a"
    lock_1, lock_2 = lock_type(str(lock_path)), lock_type(str(lock_path))

    lock_1.acquire()
    assert lock_1.is_locked
    assert not lock_2.is_locked

    with pytest.raises(Timeout, match=r"The file lock '.*' could not be acquired."):
        lock_2.acquire(timeout=0.1)
    assert not lock_2.is_locked
    assert lock_1.is_locked

    lock_1.release()
    assert not lock_1.is_locked
    assert not lock_2.is_locked


@pytest.mark.parametrize("lock_type", [FileLock, SoftFileLock])
def test_non_blocking(lock_type: type[BaseFileLock], tmp_path: Path) -> None:
    lock_path = tmp_path / "a"
    lock_1, lock_2 = lock_type(str(lock_path)), lock_type(str(lock_path))
    lock_3 = lock_type(str(lock_path), blocking=False)
    lock_4 = lock_type(str(lock_path), timeout=0)
    lock_5 = lock_type(str(lock_path), blocking=False, timeout=-1)

    lock_1.acquire()
    assert lock_1.is_locked
    assert not lock_2.is_locked
    assert not lock_3.is_locked
    assert not lock_4.is_locked
    assert not lock_5.is_locked

    with pytest.raises(Timeout, match=r"The file lock '.*' could not be acquired."):
        lock_2.acquire(blocking=False)
    assert not lock_2.is_locked
    assert lock_1.is_locked

    with pytest.raises(Timeout, match=r"The file lock '.*' could not be acquired."):
        lock_3.acquire()
    assert not lock_3.is_locked
    assert lock_1.is_locked

    with pytest.raises(Timeout, match=r"The file lock '.*' could not be acquired."), lock_3:
        pass
    assert not lock_3.is_locked
    assert lock_1.is_locked

    with pytest.raises(Timeout, match=r"The file lock '.*' could not be acquired."):
        lock_4.acquire()
    assert not lock_4.is_locked
    assert lock_1.is_locked

    with pytest.raises(Timeout, match=r"The file lock '.*' could not be acquired."), lock_4:
        pass
    assert not lock_4.is_locked
    assert lock_1.is_locked

    # blocking precedence over timeout
    with pytest.raises(Timeout, match=r"The file lock '.*' could not be acquired."):
        lock_5.acquire()
    assert not lock_5.is_locked
    assert lock_1.is_locked

    with pytest.raises(Timeout, match=r"The file lock '.*' could not be acquired."), lock_5:
        pass
    assert not lock_5.is_locked
    assert lock_1.is_locked

    lock_1.release()
    assert not lock_1.is_locked
    assert not lock_2.is_locked
    assert not lock_3.is_locked
    assert not lock_4.is_locked
    assert not lock_5.is_locked


@pytest.mark.parametrize("lock_type", [FileLock, SoftFileLock])
def test_default_timeout(lock_type: type[BaseFileLock], tmp_path: Path) -> None:
    lock_path = tmp_path / "a"
    lock_1, lock_2 = lock_type(str(lock_path)), lock_type(str(lock_path), timeout=0.1)
    assert lock_2.timeout == pytest.approx(0.1)

    lock_1.acquire()
    assert lock_1.is_locked
    assert not lock_2.is_locked

    with pytest.raises(Timeout, match=r"The file lock '.*' could not be acquired."):
        lock_2.acquire()
    assert not lock_2.is_locked
    assert lock_1.is_locked

    lock_2.timeout = 0
    assert lock_2.timeout == 0

    with pytest.raises(Timeout, match=r"The file lock '.*' could not be acquired."):
        lock_2.acquire()
    assert not lock_2.is_locked
    assert lock_1.is_locked

    lock_1.release()
    assert not lock_1.is_locked
    assert not lock_2.is_locked


@pytest.mark.parametrize("lock_type", [FileLock, SoftFileLock])
def test_context_release_on_exc(lock_type: type[BaseFileLock], tmp_path: Path) -> None:
    lock_path = tmp_path / "a"
    lock = lock_type(str(lock_path))

    try:
        with lock as lock_1:
            assert lock is lock_1
            assert lock.is_locked
            raise ValueError  # noqa: TRY301
    except ValueError:
        assert not lock.is_locked


@pytest.mark.parametrize("lock_type", [FileLock, SoftFileLock])
def test_acquire_release_on_exc(lock_type: type[BaseFileLock], tmp_path: Path) -> None:
    lock_path = tmp_path / "a"
    lock = lock_type(str(lock_path))

    try:
        with lock.acquire() as lock_1:
            assert lock is lock_1
            assert lock.is_locked
            raise ValueError  # noqa: TRY301
    except ValueError:
        assert not lock.is_locked


@pytest.mark.skipif(hasattr(sys, "pypy_version_info"), reason="del() does not trigger GC in PyPy")
@pytest.mark.parametrize("lock_type", [FileLock, SoftFileLock])
def test_del(lock_type: type[BaseFileLock], tmp_path: Path) -> None:
    lock_path = tmp_path / "a"
    lock_1, lock_2 = lock_type(str(lock_path)), lock_type(str(lock_path))

    lock_1.acquire()
    assert lock_1.is_locked
    assert not lock_2.is_locked

    with pytest.raises(Timeout, match=r"The file lock '.*' could not be acquired."):
        lock_2.acquire(timeout=0.1)

    del lock_1

    lock_2.acquire()
    assert lock_2.is_locked

    lock_2.release()


def test_cleanup_soft_lock(tmp_path: Path) -> None:
    lock_path = tmp_path / "a"

    with SoftFileLock(lock_path):
        assert lock_path.exists()
    assert not lock_path.exists()


@pytest.mark.parametrize("lock_type", [FileLock, SoftFileLock])
def test_poll_intervall_deprecated(lock_type: type[BaseFileLock], tmp_path: Path) -> None:
    lock_path = tmp_path / "a"
    lock = lock_type(str(lock_path))

    with pytest.deprecated_call(match="use poll_interval instead of poll_intervall") as checker:
        lock.acquire(poll_intervall=0.05)
        frame_info = getframeinfo(stack()[0][0])  # lineno here is one past the acquire() call above
        for warning in checker:
            if warning.filename == frame_info.filename and warning.lineno + 1 == frame_info.lineno:  # pragma: no cover
                break
        else:  # pragma: no cover
            pytest.fail("No warnings of stacklevel=2 matching.")


@pytest.mark.parametrize("lock_type", [FileLock, SoftFileLock])
def test_default_poll_interval(lock_type: type[BaseFileLock], tmp_path: Path) -> None:
    lock_path = tmp_path / "a"
    lock = lock_type(str(lock_path))
    assert lock.poll_interval == pytest.approx(0.05)

    lock_2 = lock_type(str(lock_path), poll_interval=0.1)
    assert lock_2.poll_interval == pytest.approx(0.1)

    lock_2.poll_interval = 0.2
    assert lock_2.poll_interval == pytest.approx(0.2)


@pytest.mark.parametrize("lock_type", [FileLock, SoftFileLock])
def test_poll_interval_used_by_context_manager(
    lock_type: type[BaseFileLock], tmp_path: Path, mocker: MockerFixture
) -> None:
    lock_path = tmp_path / "a"
    lock_1 = lock_type(str(lock_path))
    lock_2 = lock_type(str(lock_path), timeout=0.2, poll_interval=0.05)

    lock_1.acquire()
    sleep_mock = mocker.patch("filelock._api.time.sleep")
    with pytest.raises(Timeout):
        lock_2.acquire()
    sleep_mock.assert_called_with(0.05)

    sleep_mock.reset_mock()
    lock_2.poll_interval = 0.1
    with pytest.raises(Timeout):
        lock_2.acquire()
    sleep_mock.assert_called_with(0.1)
    lock_1.release()


@pytest.mark.parametrize("lock_type", [FileLock, SoftFileLock])
def test_poll_interval_acquire_override(lock_type: type[BaseFileLock], tmp_path: Path, mocker: MockerFixture) -> None:
    lock_path = tmp_path / "a"
    lock_1 = lock_type(str(lock_path))
    lock_2 = lock_type(str(lock_path), timeout=0.2, poll_interval=0.05)

    lock_1.acquire()
    sleep_mock = mocker.patch("filelock._api.time.sleep")
    with pytest.raises(Timeout):
        lock_2.acquire(poll_interval=0.15)
    sleep_mock.assert_called_with(0.15)
    lock_1.release()


@pytest.mark.parametrize("lock_type", [FileLock, SoftFileLock])
def test_context_decorator(lock_type: type[BaseFileLock], tmp_path: Path) -> None:
    lock_path = tmp_path / "a"
    lock = lock_type(str(lock_path))

    @lock
    def decorated_method() -> None:
        assert lock.is_locked

    assert not lock.is_locked
    decorated_method()
    assert not lock.is_locked


def test_lock_mode(tmp_path: Path) -> None:
    lock_path = tmp_path / "a.lock"
    lock = FileLock(str(lock_path), mode=0o666)

    initial_umask = os.umask(0o022)  # pin umask so the resulting permissions are predictable
    try:
        lock.acquire()
        assert lock.is_locked

        assert filemode(lock_path.stat().st_mode) == "-rw-rw-rw-"
    finally:
        os.umask(initial_umask)

    lock.release()


def test_lock_mode_soft(tmp_path: Path) -> None:
    lock_path = tmp_path / "a.lock"
    lock = SoftFileLock(str(lock_path), mode=0o666)

    initial_umask = os.umask(0o022)  # pin umask so the resulting permissions are predictable
    try:
        lock.acquire()
        assert lock.is_locked

        assert filemode(lock_path.stat().st_mode) == ("-rw-rw-rw-" if sys.platform == "win32" else "-rw-r--r--")
    finally:
        os.umask(initial_umask)

    lock.release()


def test_umask(tmp_path: Path) -> None:
    lock_path = tmp_path / "a.lock"
    lock = FileLock(str(lock_path), mode=0o666)

    initial_umask = os.umask(0)
    os.umask(initial_umask)

    lock.acquire()
    assert lock.is_locked

    current_umask = os.umask(0)
    os.umask(current_umask)
    assert initial_umask == current_umask

    lock.release()


def test_umask_soft(tmp_path: Path) -> None:
    lock_path = tmp_path / "a.lock"
    lock = SoftFileLock(str(lock_path), mode=0o666)

    initial_umask = os.umask(0)
    os.umask(initial_umask)

    lock.acquire()
    assert lock.is_locked

    current_umask = os.umask(0)
    os.umask(current_umask)
    assert initial_umask == current_umask

    lock.release()


def test_wrong_platform(tmp_path: Path) -> None:
    assert not inspect.isabstract(UnixFileLock)
    assert not inspect.isabstract(WindowsFileLock)
    assert inspect.isabstract(BaseFileLock)

    lock_type = UnixFileLock if sys.platform == "win32" else WindowsFileLock
    lock = lock_type(tmp_path / "lockfile")

    with pytest.raises(NotImplementedError):
        lock.acquire()
    with pytest.raises(NotImplementedError):
        lock._release()


@pytest.mark.skipif(sys.platform == "win32", reason="flock not run on windows")
@pytest.mark.filterwarnings("default::UserWarning")
def test_flock_not_implemented_unix(tmp_path: Path, mocker: MockerFixture) -> None:
    mocker.patch("fcntl.flock", side_effect=OSError(ENOSYS, "mock error"))
    lock = FileLock(tmp_path / "a.lock")
    with lock:
        assert lock.is_locked
        assert isinstance(lock, SoftFileLock)


def test_soft_errors(tmp_path: Path, mocker: MockerFixture) -> None:
    mocker.patch("os.open", side_effect=OSError(ENOSYS, "mock error"))
    with pytest.raises(OSError, match="mock error"):
        SoftFileLock(tmp_path / "a.lock").acquire()


def _check_file_read_write(txt_file: Path) -> None:
    for _ in range(3):
        uuid = str(uuid4())
        txt_file.write_text(uuid, encoding="utf-8")
        assert txt_file.read_text(encoding="utf-8") == uuid


@pytest.mark.parametrize("lock_type", [FileLock, SoftFileLock])
def test_thrashing_with_thread_pool_passing_lock_to_threads(tmp_path: Path, lock_type: type[BaseFileLock]) -> None:
    def mess_with_file(lock_: BaseFileLock) -> None:
        with lock_:
            _check_file_read_write(txt_file)

    lock_file, txt_file = tmp_path / "test.txt.lock", tmp_path / "test.txt"
    lock = lock_type(lock_file)
    with ThreadPoolExecutor() as executor:
        results = [executor.submit(mess_with_file, lock) for _ in range(100)]
    assert all(r.result() is None for r in results)


@pytest.mark.parametrize("lock_type", [FileLock, SoftFileLock])
def test_thrashing_with_thread_pool_global_lock(tmp_path: Path, lock_type: type[BaseFileLock]) -> None:
    def mess_with_file() -> None:
        with lock:
            _check_file_read_write(txt_file)

    lock_file, txt_file = tmp_path / "test.txt.lock", tmp_path / "test.txt"
    lock = lock_type(lock_file)
    with ThreadPoolExecutor() as executor:
        results = [executor.submit(mess_with_file) for _ in range(100)]

    assert all(r.result() is None for r in results)


@pytest.mark.parametrize("lock_type", [FileLock, SoftFileLock])
def test_thrashing_with_thread_pool_lock_recreated_in_each_thread(
    tmp_path: Path,
    lock_type: type[BaseFileLock],
) -> None:
    def mess_with_file() -> None:
        with lock_type(lock_file):
            _check_file_read_write(txt_file)

    lock_file, txt_file = tmp_path / "test.txt.lock", tmp_path / "test.txt"
    with ThreadPoolExecutor() as executor:
        results = [executor.submit(mess_with_file) for _ in range(100)]

    assert all(r.result() is None for r in results)


@pytest.mark.parametrize("lock_type", [FileLock, SoftFileLock])
def test_lock_can_be_non_thread_local(
    tmp_path: Path,
    lock_type: type[BaseFileLock],
) -> None:
    lock = lock_type(tmp_path / "test.lock", thread_local=False)

    for _ in range(2):
        thread = threading.Thread(target=lock.acquire, kwargs={"timeout": 2})
        thread.start()
        thread.join()

    assert lock.lock_counter == 2

    lock.release(force=True)


@pytest.mark.parametrize("lock_type", [FileLock, SoftFileLock])
def test_thread_local_setter_visibility(lock_type: type[BaseFileLock], tmp_path: Path) -> None:
    """Property setters stay per-thread when thread_local=True.

    A ``poll_interval`` set on the constructing thread stays invisible to other threads: ``threading.local`` re-applies
    the constructor arguments the first time each new thread touches the context, so the reader sees the default.
    """
    lock = lock_type(tmp_path / "x.lock", thread_local=True, poll_interval=0.05)
    lock.poll_interval = 0.5

    observed: list[float] = []

    def read_from_thread() -> None:
        observed.append(lock.poll_interval)

    t = threading.Thread(target=read_from_thread)
    t.start()
    t.join()

    assert observed == [pytest.approx(0.05)]


@pytest.mark.parametrize("lock_type", [FileLock, SoftFileLock])
def test_non_thread_local_setter_visibility(lock_type: type[BaseFileLock], tmp_path: Path) -> None:
    """With thread_local=False, property setters are visible across threads."""
    lock = lock_type(tmp_path / "x.lock", thread_local=False, poll_interval=0.05)
    lock.poll_interval = 0.5

    observed: list[float] = []
    t = threading.Thread(target=lambda: observed.append(lock.poll_interval))
    t.start()
    t.join()

    assert observed == [pytest.approx(0.5)]


def test_subclass_compatibility(tmp_path: Path) -> None:
    class MyFileLock(FileLock):
        def __init__(
            self,
            lock_file: str | os.PathLike[str],
            timeout: float = -1,
            mode: int = 0o644,
            thread_local: bool = True,
            my_param: int = 0,
            **kwargs: dict[str, Any],  # noqa: ARG002
        ) -> None:
            super().__init__(lock_file, timeout, mode, thread_local, is_singleton=True)
            self.blocking = True
            self.my_param = my_param

    lock_path = tmp_path / "a"
    MyFileLock(str(lock_path), my_param=1)

    class MySoftFileLock(SoftFileLock):
        def __init__(
            self,
            lock_file: str | os.PathLike[str],
            timeout: float = -1,
            mode: int = 0o644,
            thread_local: bool = True,
            my_param: int = 0,
            **kwargs: dict[str, Any],  # noqa: ARG002
        ) -> None:
            super().__init__(lock_file, timeout, mode, thread_local, blocking=True, is_singleton=True)
            self.my_param = my_param

    MySoftFileLock(str(lock_path), my_param=1)


@pytest.mark.parametrize("lock_type", [FileLock, SoftFileLock])
def test_singleton_and_non_singleton_locks_are_distinct(lock_type: type[BaseFileLock], tmp_path: Path) -> None:
    lock_path = tmp_path / "a"
    lock_1 = lock_type(str(lock_path), is_singleton=False)
    assert lock_1.is_singleton is False

    lock_2 = lock_type(str(lock_path), is_singleton=True)
    assert lock_2.is_singleton is True
    assert lock_2 is not lock_1


@pytest.mark.parametrize("lock_type", [FileLock, SoftFileLock])
def test_singleton_locks_are_the_same(lock_type: type[BaseFileLock], tmp_path: Path) -> None:
    lock_path = tmp_path / "a"
    lock_1 = lock_type(str(lock_path), is_singleton=True)

    lock_2 = lock_type(str(lock_path), is_singleton=True)
    assert lock_2 is lock_1


def test_singleton_locks_survive_concurrent_first_construction(tmp_path: Path) -> None:
    # Two threads constructing the same is_singleton=True lock at once must still share one instance. A slow
    # __init__ widens the window between the cache miss and the store so the race is hit deterministically.
    lock_path = tmp_path / "a"

    class _SlowLock(SoftFileLock):
        def __init__(self, lock_file: str, *, is_singleton: bool = True) -> None:
            time.sleep(0.05)
            super().__init__(lock_file, is_singleton=is_singleton)

    results: list[BaseFileLock] = []
    barrier = threading.Barrier(2)

    def build() -> None:
        barrier.wait()
        results.append(_SlowLock(str(lock_path), is_singleton=True))

    threads = [threading.Thread(target=build) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert results[0] is results[1]


@pytest.mark.parametrize("lock_type", [FileLock, SoftFileLock])
def test_singleton_locks_are_distinct_per_lock_file(lock_type: type[BaseFileLock], tmp_path: Path) -> None:
    lock_path_1 = tmp_path / "a"
    lock_1 = lock_type(str(lock_path_1), is_singleton=True)

    lock_path_2 = tmp_path / "b"
    lock_2 = lock_type(str(lock_path_2), is_singleton=True)
    assert lock_1 is not lock_2


@pytest.mark.parametrize("lock_type", [FileLock, SoftFileLock])
def test_singleton_locks_must_be_initialized_with_the_same_args(lock_type: type[BaseFileLock], tmp_path: Path) -> None:
    lock_path = tmp_path / "a"
    args: dict[str, Any] = {"timeout": -1, "mode": 0o644, "thread_local": True, "blocking": True, "poll_interval": 0.05}
    alternate_args: dict[str, Any] = {
        "timeout": 10,
        "mode": 0,
        "thread_local": False,
        "blocking": False,
        "poll_interval": 0.1,
    }

    lock = lock_type(str(lock_path), is_singleton=True, **args)

    general_msg = "Singleton lock instances cannot be initialized with differing arguments"
    for arg_name in args:
        altered_args = args.copy()
        altered_args[arg_name] = alternate_args[arg_name]
        with pytest.raises(ValueError, match=general_msg) as exc_info:
            lock_type(str(lock_path), is_singleton=True, **altered_args)
        exc_info.match(arg_name)  # ensure specific non-matching argument is included in exception text
    del lock, exc_info


@pytest.mark.skipif(hasattr(sys, "pypy_version_info"), reason="del() does not trigger GC in PyPy")
@pytest.mark.parametrize("lock_type", [FileLock, SoftFileLock])
def test_singleton_locks_are_deleted_when_no_external_references_exist(
    lock_type: type[BaseFileLock],
    tmp_path: Path,
) -> None:
    lock_path = tmp_path / "a"
    lock = lock_type(str(lock_path), is_singleton=True)

    assert lock_type._instances == {str(lock_path): lock}
    del lock
    assert lock_type._instances == {}


@pytest.mark.skipif(hasattr(sys, "pypy_version_info"), reason="del() does not trigger GC in PyPy")
@pytest.mark.parametrize("lock_type", [FileLock, SoftFileLock])
def test_singleton_instance_tracking_is_unique_per_subclass(lock_type: type[BaseFileLock]) -> None:
    class Lock1(lock_type):  # ty: ignore[unsupported-base]
        pass

    class Lock2(lock_type):  # ty: ignore[unsupported-base]
        pass

    assert isinstance(Lock1._instances, WeakValueDictionary)
    assert isinstance(Lock2._instances, WeakValueDictionary)
    assert Lock1._instances is not Lock2._instances


def test_singleton_locks_when_inheriting_init_is_called_once(tmp_path: Path) -> None:
    init_calls = 0

    class MyFileLock(FileLock):
        def __init__(self, *args: Any, **kwargs: Any) -> None:  # noqa: ANN401
            super().__init__(*args, **kwargs)
            nonlocal init_calls
            init_calls += 1

    lock_path = tmp_path / "a"
    lock1 = MyFileLock(str(lock_path), is_singleton=True)
    lock2 = MyFileLock(str(lock_path), is_singleton=True)

    assert lock1 is lock2
    assert init_calls == 1


def test_file_lock_positional_argument(tmp_path: Path) -> None:
    class FilePathLock(FileLock):
        def __init__(self, file_path: str) -> None:
            super().__init__(file_path + ".lock")

    lock_path = tmp_path / "a"
    lock = FilePathLock(str(lock_path))
    assert lock.lock_file == str(lock_path) + ".lock"


@pytest.mark.skipif(sys.platform != "win32" and os.geteuid() == 0, reason="root can open a 0o444 file for writing")
@pytest.mark.parametrize("lock_type", [SoftFileLock, FileLock])
def test_readonly_lock_file_with_mtime_zero_raises(lock_type: type[BaseFileLock], tmp_path: Path) -> None:
    # acquire() no longer short-circuits the writability check on mtime 0, so a read-only lock file is still rejected.
    lock_path = tmp_path / "z.lock"
    lock_path.touch()
    lock_path.chmod(0o444)
    os.utime(lock_path, (0, 0))
    try:
        with pytest.raises(PermissionError):
            lock_type(str(lock_path)).acquire(timeout=0)
    finally:
        lock_path.chmod(0o644)


@pytest.mark.parametrize("lock_type", [SoftFileLock])
def test_lock_file_removed_after_release(tmp_path: Path, lock_type: type[BaseFileLock]) -> None:
    lock_path = tmp_path / "test.lock"
    lock = lock_type(str(lock_path))
    with lock:
        assert lock_path.exists()
    assert not lock_path.exists()


@pytest.mark.skipif(sys.platform == "win32", reason="Unix flock semantics")
def test_concurrent_acquire_release_keeps_lock_file(tmp_path: Path) -> None:
    lock_path = tmp_path / "test.lock"
    errors: list[Exception] = []

    def worker() -> None:
        try:
            for _ in range(20):
                lock = FileLock(str(lock_path), is_singleton=False)
                with lock:
                    pass
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert not errors, errors
    assert lock_path.exists()


@pytest.mark.skipif(sys.platform == "win32", reason="Unix flock semantics")
def test_lock_acquired_after_release_keeps_path(tmp_path: Path) -> None:
    lock_path = tmp_path / "test.lock"
    first = FileLock(str(lock_path), is_singleton=False)
    second = FileLock(str(lock_path), is_singleton=False)

    first.acquire()
    assert lock_path.exists()
    first.release()
    assert lock_path.exists()

    second.acquire()
    assert lock_path.exists()
    second.release()
    assert lock_path.exists()


def test_waiter_fd_cannot_split_lock_after_release(tmp_path: Path) -> None:
    if sys.platform == "win32":  # pragma: win32 cover  # Unix flock semantics; also narrows fcntl for the type checker
        return
    import fcntl

    lock_path = tmp_path / "test.lock"
    first = FileLock(str(lock_path), is_singleton=False)
    replacement = FileLock(str(lock_path), is_singleton=False)

    first.acquire()
    waiter_fd = os.open(lock_path, os.O_RDWR)

    try:
        first.release()
        assert lock_path.exists()

        replacement.acquire()
        try:
            with pytest.raises(BlockingIOError) as exc_info:
                fcntl.flock(waiter_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            assert exc_info.value.errno in {EAGAIN, EWOULDBLOCK}
        finally:
            replacement.release()
    finally:
        os.close(waiter_fd)


@pytest.mark.skipif(sys.platform == "win32", reason="Unix flock semantics")
def test_stale_inode_retry_on_unlinked_lock(tmp_path: Path, mocker: MockerFixture) -> None:
    lock_path = tmp_path / "test.lock"
    lock = FileLock(str(lock_path), is_singleton=False)

    real_fstat = os.fstat
    call_count = 0

    def fstat_unlinked_once(fd: int) -> os.stat_result:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            Path(lock_path).unlink()
            return real_fstat(fd)
        return real_fstat(fd)

    mocker.patch("os.fstat", side_effect=fstat_unlinked_once)
    lock.acquire()
    assert lock.is_locked
    assert call_count == 2
    lock.release()


@pytest.mark.skipif(sys.platform == "win32", reason="Unix flock semantics")
def test_permission_error_fallback_without_o_creat(tmp_path: Path, mocker: MockerFixture) -> None:
    lock_path = tmp_path / "test.lock"
    lock_path.touch()
    lock = FileLock(str(lock_path), is_singleton=False)

    real_open = os.open
    call_count = 0

    def open_no_creat(path: str, flags: int, mode: int = 0o777, *, dir_fd: int | None = None) -> int:
        nonlocal call_count
        call_count += 1
        if call_count == 1 and flags & os.O_CREAT:
            raise PermissionError(13, "Permission denied", path)
        return real_open(path, flags, mode) if dir_fd is None else real_open(path, flags, mode, dir_fd=dir_fd)

    mocker.patch("os.open", side_effect=open_no_creat)
    lock.acquire()
    assert lock.is_locked
    assert call_count == 2
    lock.release()


@pytest.mark.skipif(sys.platform == "win32", reason="Unix flock semantics")
def test_permission_error_propagates_when_file_missing(tmp_path: Path, mocker: MockerFixture) -> None:
    lock_path = tmp_path / "test.lock"
    lock = FileLock(str(lock_path), is_singleton=False)

    real_open = os.open

    def open_always_permission_error(path: str, flags: int, mode: int = 0o777, *, dir_fd: int | None = None) -> int:
        if "test.lock" in path:
            raise PermissionError(13, "Permission denied", path)
        return real_open(path, flags, mode) if dir_fd is None else real_open(path, flags, mode, dir_fd=dir_fd)

    mocker.patch("os.open", side_effect=open_always_permission_error)
    with pytest.raises(PermissionError, match="Permission denied"):
        lock.acquire(timeout=0)


@pytest.mark.skipif(sys.platform == "win32", reason="Unix flock semantics")
def test_sticky_bit_fallback_handles_concurrent_unlink(tmp_path: Path, mocker: MockerFixture) -> None:
    lock_path = tmp_path / "test.lock"
    lock_path.touch()
    lock = FileLock(str(lock_path), is_singleton=False)

    real_open = os.open
    call_count = 0

    def open_permission_then_unlink(path: str, flags: int, mode: int = 0o777, *, dir_fd: int | None = None) -> int:
        nonlocal call_count
        call_count += 1
        if call_count == 1 and flags & os.O_CREAT and "test.lock" in path:
            raise PermissionError(13, "Permission denied", path)
        if call_count == 2 and not (flags & os.O_CREAT) and "test.lock" in path:
            lock_path.unlink(missing_ok=True)
            raise FileNotFoundError(2, "No such file or directory", path)
        return real_open(path, flags, mode) if dir_fd is None else real_open(path, flags, mode, dir_fd=dir_fd)

    mocker.patch("os.open", side_effect=open_permission_then_unlink)
    lock.acquire()
    assert lock.is_locked
    assert call_count == 3
    lock.release()


@pytest.mark.parametrize("lock_type", [FileLock, SoftFileLock])
def test_cancel_check_triggers(lock_type: type[BaseFileLock], tmp_path: Path) -> None:
    lock_path = tmp_path / "a"
    lock_1 = lock_type(str(lock_path))
    lock_2 = lock_type(str(lock_path))

    lock_1.acquire()

    with pytest.raises(Timeout, match=r"The file lock '.*' could not be acquired."):
        lock_2.acquire(timeout=1, cancel_check=lambda: True)
    assert not lock_2.is_locked
    lock_1.release()


@pytest.mark.parametrize("lock_type", [FileLock, SoftFileLock])
def test_cancel_check_after_n_polls(lock_type: type[BaseFileLock], tmp_path: Path, mocker: MockerFixture) -> None:
    lock_path = tmp_path / "a"
    lock_1 = lock_type(str(lock_path))
    lock_2 = lock_type(str(lock_path))

    lock_1.acquire()

    call_count = 0

    def cancel_after_two() -> bool:
        nonlocal call_count
        call_count += 1
        return call_count >= 2

    mocker.patch("filelock._api.time.sleep")
    with pytest.raises(Timeout):
        lock_2.acquire(timeout=10, cancel_check=cancel_after_two)
    assert call_count == 2
    assert not lock_2.is_locked
    lock_1.release()


@pytest.mark.parametrize("lock_type", [FileLock, SoftFileLock])
def test_cancel_check_not_called_when_lock_available(lock_type: type[BaseFileLock], tmp_path: Path) -> None:
    lock_path = tmp_path / "a"
    lock = lock_type(str(lock_path))

    called = False

    def should_not_be_called() -> bool:
        nonlocal called
        called = True
        return True

    lock.acquire(cancel_check=should_not_be_called)
    assert lock.is_locked
    assert not called
    lock.release()


@pytest.mark.parametrize("lock_type", [FileLock, SoftFileLock])
def test_cancel_check_false_allows_acquisition(lock_type: type[BaseFileLock], tmp_path: Path) -> None:
    lock_path = tmp_path / "a"
    lock = lock_type(str(lock_path))

    lock.acquire(cancel_check=lambda: False)
    assert lock.is_locked
    lock.release()


@pytest.mark.parametrize("lock_type", [FileLock, SoftFileLock])
def test_cancel_check_log_message(
    lock_type: type[BaseFileLock], tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    lock_path = tmp_path / "a"
    lock_1 = lock_type(str(lock_path))
    lock_2 = lock_type(str(lock_path))

    lock_1.acquire()
    with pytest.raises(Timeout):
        lock_2.acquire(timeout=1, cancel_check=lambda: True)
    assert any("Cancellation requested" in msg for msg in caplog.messages)
    lock_1.release()


@pytest.mark.skipif(sys.platform == "win32", reason="unix-only test")
def test_filenotfound_on_fuse_nfs_retries(tmp_path: Path, mocker: MockerFixture) -> None:
    """Retry recovers from the FUSE/NFS os.open(O_CREAT) race that raises FileNotFoundError."""
    lock_path = tmp_path / "test.lock"
    lock = FileLock(str(lock_path), is_singleton=False)

    real_open = os.open
    call_count = 0

    def open_enoent_then_succeed(path: str, flags: int, mode: int = 0o777, *, dir_fd: int | None = None) -> int:
        nonlocal call_count
        call_count += 1
        if call_count == 1 and flags & os.O_CREAT and "test.lock" in path:  # first O_CREAT hits the FUSE/NFS ENOENT
            raise FileNotFoundError(2, "No such file or directory", path)
        return real_open(path, flags, mode) if dir_fd is None else real_open(path, flags, mode, dir_fd=dir_fd)

    mocker.patch("os.open", side_effect=open_enoent_then_succeed)
    lock.acquire()
    assert lock.is_locked
    assert call_count >= 2
    lock.release()


@pytest.mark.parametrize("lock_type", [FileLock, SoftFileLock])
def test_same_thread_different_instances_raises(tmp_path: Path, lock_type: type[BaseFileLock]) -> None:
    lock_path = tmp_path / "test.lock"
    lock1 = lock_type(lock_path)
    with lock1:
        lock2 = lock_type(lock_path)
        with pytest.raises(RuntimeError, match="Deadlock"):
            lock2.acquire()


@pytest.mark.parametrize("lock_type", [FileLock, SoftFileLock])
def test_finite_timeout_gives_timeout_not_deadlock(tmp_path: Path, lock_type: type[BaseFileLock]) -> None:
    lock_path = tmp_path / "test.lock"
    lock1 = lock_type(lock_path)
    with lock1:
        lock2 = lock_type(lock_path, timeout=0.1)
        with pytest.raises(Timeout):
            lock2.acquire()


@pytest.mark.parametrize("lock_type", [FileLock, SoftFileLock])
def test_non_blocking_gives_timeout_not_deadlock(tmp_path: Path, lock_type: type[BaseFileLock]) -> None:
    lock_path = tmp_path / "test.lock"
    lock1 = lock_type(lock_path)
    with lock1:
        lock2 = lock_type(lock_path, blocking=False)
        with pytest.raises(Timeout):
            lock2.acquire()


@pytest.mark.parametrize("lock_type", [FileLock, SoftFileLock])
def test_different_paths_no_conflict(tmp_path: Path, lock_type: type[BaseFileLock]) -> None:
    lock1 = lock_type(tmp_path / "a.lock")
    lock2 = lock_type(tmp_path / "b.lock")
    with lock1, lock2:
        assert lock1.is_locked
        assert lock2.is_locked


@pytest.mark.parametrize("lock_type", [FileLock, SoftFileLock])
def test_same_instance_reentrant_works(tmp_path: Path, lock_type: type[BaseFileLock]) -> None:
    lock = lock_type(tmp_path / "test.lock")
    with lock:
        with lock:
            assert lock.is_locked
        assert lock.is_locked
    assert not lock.is_locked


@pytest.mark.parametrize("lock_type", [FileLock, SoftFileLock])
def test_singleton_avoids_deadlock(tmp_path: Path, lock_type: type[BaseFileLock]) -> None:
    lock_path = tmp_path / "test.lock"
    lock1 = lock_type(lock_path, is_singleton=True)
    with lock1:
        lock2 = lock_type(lock_path, is_singleton=True)
        assert lock1 is lock2
        with lock2:
            assert lock2.is_locked


@pytest.mark.parametrize("lock_type", [FileLock, SoftFileLock])
def test_different_threads_no_false_positive(tmp_path: Path, lock_type: type[BaseFileLock]) -> None:
    lock_path = tmp_path / "test.lock"
    lock1 = lock_type(lock_path, timeout=0)
    lock1.acquire()

    error: BaseException | None = None

    def acquire_in_thread() -> None:
        nonlocal error
        lock2 = lock_type(lock_path, timeout=0)
        try:
            lock2.acquire()
        except BaseException as exc:
            error = exc

    thread = threading.Thread(target=acquire_in_thread)
    thread.start()
    thread.join()
    lock1.release()

    assert not isinstance(error, RuntimeError), "Should not raise RuntimeError in different thread"


@pytest.mark.skipif(sys.platform == "win32", reason="unix-only symlink test")
@pytest.mark.parametrize("lock_type", [FileLock, SoftFileLock])
def test_symlink_same_canonical_path(tmp_path: Path, lock_type: type[BaseFileLock]) -> None:
    lock_path = tmp_path / "test.lock"
    symlink_path = tmp_path / "link.lock"
    symlink_path.symlink_to(lock_path)

    lock1 = lock_type(lock_path)
    with lock1:
        lock2 = lock_type(symlink_path)
        with pytest.raises(RuntimeError, match="Deadlock"):
            lock2.acquire()


@pytest.mark.parametrize("lock_type", [FileLock, SoftFileLock])
def test_deadlock_registry_cleanup_on_release(tmp_path: Path, lock_type: type[BaseFileLock]) -> None:
    lock_path = tmp_path / "test.lock"
    lock1 = lock_type(lock_path)
    lock1.acquire()
    lock1.release()

    lock2 = lock_type(lock_path)
    with lock2:
        assert lock2.is_locked


@pytest.mark.parametrize("lock_type", [FileLock, SoftFileLock])
def test_force_release_cleans_registry(tmp_path: Path, lock_type: type[BaseFileLock]) -> None:
    lock_path = tmp_path / "test.lock"
    lock1 = lock_type(lock_path)
    with lock1:
        lock1.acquire()
        assert lock1.lock_counter == 2
    lock1.release(force=True)

    lock2 = lock_type(lock_path)
    with lock2:
        assert lock2.is_locked


@pytest.fixture
def held_lock_path(tmp_path: Path) -> Iterator[Path]:
    path = tmp_path / "resource.lock"
    with FileLock(path, timeout=0):
        yield path


@_UNIX_FLOCK_ONLY
def test_winner_truncates_stale_content(tmp_path: Path) -> None:
    lock_path = tmp_path / "resource.lock"
    lock_path.write_text("stale from a previous holder", encoding="utf-8")

    with FileLock(lock_path, timeout=0):
        assert lock_path.stat().st_size == 0


@_UNIX_FLOCK_ONLY
def test_contender_preserves_holder_contents(held_lock_path: Path) -> None:
    held_lock_path.write_text("holder metadata", encoding="utf-8")

    with pytest.raises(Timeout):
        FileLock(held_lock_path, timeout=0).acquire()

    assert held_lock_path.read_text(encoding="utf-8") == "holder metadata"


@_UNIX_FLOCK_ONLY
def test_contender_preserves_holder_mode(held_lock_path: Path) -> None:
    held_lock_path.chmod(0o600)

    with pytest.raises(Timeout):
        FileLock(held_lock_path, timeout=0, mode=0o644).acquire()

    assert S_IMODE(held_lock_path.stat().st_mode) == 0o600


@_UNIX_FLOCK_ONLY
def test_contender_does_not_fchmod(held_lock_path: Path, mocker: MockerFixture) -> None:
    fchmod_spy = mocker.spy(os, "fchmod")

    with pytest.raises(Timeout):
        FileLock(held_lock_path, timeout=0, mode=0o644).acquire()

    fchmod_spy.assert_not_called()


@_UNIX_FLOCK_ONLY
def test_post_lock_truncate_failure_closes_fd(tmp_path: Path, mocker: MockerFixture) -> None:
    mocker.patch("os.ftruncate", side_effect=OSError(ENOSPC, "No space left on device"))
    open_spy = mocker.spy(os, "open")
    close_spy = mocker.spy(os, "close")

    with pytest.raises(OSError, match="No space left on device"):
        FileLock(tmp_path / "resource.lock", timeout=0).acquire()

    assert any(call.args and call.args[0] == open_spy.spy_return for call in close_spy.call_args_list)


@_WINDOWS_ONLY
def test_windows_reparse_point_lock_file_rejected(tmp_path: Path) -> None:
    target = tmp_path / "target.txt"
    target.write_text("sensitive", encoding="utf-8")
    link = tmp_path / "resource.lock"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("cannot create symlinks (needs Developer Mode or administrator)")

    with pytest.raises(OSError, match="reparse point"):
        FileLock(link).acquire()

    assert target.read_text(encoding="utf-8") == "sensitive"


def test_release_keeps_lock_until_final_hold(tmp_path: Path) -> None:
    lock = FileLock(str(tmp_path / "a"))
    lock.acquire()
    lock.acquire()
    lock.release()
    assert lock.is_locked
    assert lock.lock_counter == 1
    lock.release()
    assert not lock.is_locked
    assert lock.lock_counter == 0


def test_forced_release_drops_all_holds(tmp_path: Path) -> None:
    lock = FileLock(str(tmp_path / "a"))
    lock.acquire()
    lock.acquire()
    lock.release(force=True)
    assert not lock.is_locked
    assert lock.lock_counter == 0


@_UNIX_FLOCK_ONLY
def test_unix_release_keeps_lock_held_when_unlock_fails(tmp_path: Path, mocker: MockerFixture) -> None:
    lock = FileLock(str(tmp_path / "a"))
    lock.acquire()
    mocker.patch("filelock._unix.fcntl.flock", side_effect=[OSError(EIO, "unlock failed"), None])
    with pytest.raises(OSError, match="unlock failed"):
        lock.release()
    assert lock.is_locked
    assert lock.lock_counter == 1
    lock.release()
    assert not lock.is_locked


@_UNIX_FLOCK_ONLY
def test_unix_context_exit_propagates_unlock_failure(tmp_path: Path, mocker: MockerFixture) -> None:
    # One LOCK_EX for acquire, then one LOCK_UN per release; fail only the first unlock so __exit__ propagates it.
    mocker.patch("filelock._unix.fcntl.flock", side_effect=[None, OSError(EIO, "unlock failed"), None])
    lock = FileLock(str(tmp_path / "a"))
    with pytest.raises(OSError, match="unlock failed"), lock:
        assert lock.is_locked
    assert lock.is_locked
    lock.release()
    assert not lock.is_locked


@_WINDOWS_ONLY
def test_windows_release_keeps_lock_held_when_unlock_fails(tmp_path: Path, mocker: MockerFixture) -> None:
    lock = FileLock(str(tmp_path / "a"))
    lock.acquire()
    mocker.patch("filelock._windows.msvcrt.locking", side_effect=[OSError(EIO, "unlock failed"), None])
    with pytest.raises(OSError, match="unlock failed"):
        lock.release()
    assert lock.is_locked
    assert lock.lock_counter == 1
    lock.release()
    assert not lock.is_locked


@_WINDOWS_ONLY
def test_windows_close_failure_still_commits_release(tmp_path: Path, mocker: MockerFixture) -> None:
    lock = FileLock(str(tmp_path / "a"))
    lock.acquire()
    # The OS unlock succeeds, so the lock is released; the later close failure propagates but must not leave the
    # counter or registry believing the lock is still held.
    mocker.patch("filelock._windows.os.close", side_effect=OSError(EIO, "close failed"))
    with pytest.raises(OSError, match="close failed"):
        lock.release()
    assert not lock.is_locked
    assert lock.lock_counter == 0


@_WINDOWS_ONLY
def test_windows_delete_pending_is_treated_as_contention(tmp_path: Path, mocker: MockerFixture) -> None:
    mocker.patch("filelock._windows._nt_open", return_value=(0, 0xC0000056))  # STATUS_DELETE_PENDING
    with pytest.raises(Timeout):
        FileLock(str(tmp_path / "a"), timeout=0.2).acquire()


@_WINDOWS_ONLY
def test_windows_permanent_denial_raises_without_timeout(tmp_path: Path, mocker: MockerFixture) -> None:
    mocker.patch("filelock._windows._nt_open", return_value=(0, 0xC0000022))  # STATUS_ACCESS_DENIED
    with pytest.raises(PermissionError):
        FileLock(str(tmp_path / "a"), timeout=5).acquire()


def test_windows_delete_in_progress_is_contention_not_denial(tmp_path: Path) -> None:
    if sys.platform != "win32":
        pytest.skip("windows-only")
    import ctypes

    target = str(tmp_path / "dp.lock")
    kernel32 = ctypes.windll.kernel32
    delete_access, generic_read, share_all, create_always, delete_on_close = (
        0x10000,
        0x80000000,
        0x1 | 0x2 | 0x4,
        2,
        0x04000000,
    )
    # Open with delete-on-close so the name is marked for deletion but the live handle keeps it around: a fresh open
    # sees the deletion in progress (delete-pending or a sharing violation), which acquire must retry rather than
    # mistake for a permanent PermissionError.
    handle = kernel32.CreateFileW(
        target, delete_access | generic_read, share_all, None, create_always, delete_on_close, None
    )
    assert handle not in {0, ctypes.c_void_p(-1).value}
    try:
        with pytest.raises(Timeout):
            FileLock(target, timeout=0.3).acquire()
    finally:
        kernel32.CloseHandle(handle)


def _enter_direct(lock: SoftFileLock) -> AbstractContextManager[object]:
    return lock


def _enter_proxy(lock: SoftFileLock) -> AbstractContextManager[object]:
    return lock.acquire()  # exercises AcquireReturnProxy.__exit__ instead of BaseFileLock.__exit__


_CONTEXT_ENTRIES: Final = [pytest.param(_enter_direct, id="direct"), pytest.param(_enter_proxy, id="proxy")]


@pytest.mark.parametrize("enter", _CONTEXT_ENTRIES)
def test_context_group_reports_both_failures(
    tmp_path: Path, mocker: MockerFixture, enter: Callable[[SoftFileLock], AbstractContextManager[object]]
) -> None:
    lock = SoftFileLock(str(tmp_path / "a"), context_error_policy="group")
    mocker.patch.object(lock, "release", side_effect=OSError("release failed"))
    with pytest.raises(ExceptionGroup) as info, enter(lock):
        raise ValueError
    body, release = info.value.exceptions
    assert isinstance(body, ValueError)
    assert isinstance(release, OSError)
    assert body.__traceback__ is not None  # leaves keep their own frames
    assert release.__traceback__ is not None


@pytest.mark.parametrize("enter", _CONTEXT_ENTRIES)
def test_context_chain_keeps_release_error_with_body_in_context(
    tmp_path: Path, mocker: MockerFixture, enter: Callable[[SoftFileLock], AbstractContextManager[object]]
) -> None:
    lock = SoftFileLock(str(tmp_path / "a"), context_error_policy="chain")
    mocker.patch.object(lock, "release", side_effect=OSError("release failed"))
    with pytest.raises(OSError, match="release failed") as info, enter(lock):
        raise ValueError
    assert isinstance(info.value.__context__, ValueError)


@pytest.mark.parametrize("policy", ["chain", "group"])
def test_context_body_only_failure_propagates_body(tmp_path: Path, policy: ContextErrorPolicy) -> None:
    lock = SoftFileLock(str(tmp_path / "a"), context_error_policy=policy)
    body = ValueError("body failed")
    with pytest.raises(ValueError, match="body failed"), lock:
        raise body


@pytest.mark.parametrize("policy", ["chain", "group"])
def test_context_release_only_failure_propagates_release(
    tmp_path: Path, mocker: MockerFixture, policy: ContextErrorPolicy
) -> None:
    lock = SoftFileLock(str(tmp_path / "a"), context_error_policy=policy)
    mocker.patch.object(lock, "release", side_effect=OSError("release failed"))
    with pytest.raises(OSError, match="release failed"), lock:
        pass


def test_context_group_base_exception_leaf_is_base_group(tmp_path: Path, mocker: MockerFixture) -> None:
    lock = SoftFileLock(str(tmp_path / "a"), context_error_policy="group")
    mocker.patch.object(lock, "release", side_effect=OSError("release failed"))
    with pytest.raises(BaseExceptionGroup) as info, lock:
        raise KeyboardInterrupt
    assert not isinstance(info.value, ExceptionGroup)  # a BaseException leaf stays outside except Exception
    assert [type(leaf) for leaf in info.value.exceptions] == [KeyboardInterrupt, OSError]


def test_invalid_context_error_policy_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="context_error_policy must be"):
        SoftFileLock(str(tmp_path / "a"), context_error_policy="explode")  # ty: ignore[invalid-argument-type]


def test_singleton_rejects_different_context_policy(tmp_path: Path) -> None:
    path = str(tmp_path / "a")
    first = SoftFileLock(path, is_singleton=True, context_error_policy="chain")
    try:
        with pytest.raises(ValueError, match="context_error_policy"):
            SoftFileLock(path, is_singleton=True, context_error_policy="group")
    finally:
        first.release(force=True)


def _fail_close_of(mocker: MockerFixture, lock: BaseFileLock, error: OSError) -> list[int]:
    # Fail os.close only for this lock's own descriptor, and record each attempt on it. A blanket patch would also hit
    # the close another test's lock runs from __del__ during this test, turning an unrelated garbage collection into a
    # spurious failure. Returns the list of attempts so a caller can assert close is not retried.
    fd = lock._context.lock_file_fd
    real_close = os.close
    attempts: list[int] = []

    def close(target: int) -> None:
        if target == fd:
            attempts.append(target)
            raise error
        real_close(target)

    mocker.patch("filelock._api.os.close", side_effect=close)
    return attempts


@_UNIX_FLOCK_ONLY
def test_close_error_default_suppressed_on_unix(tmp_path: Path, mocker: MockerFixture) -> None:
    lock = FileLock(str(tmp_path / "a"))  # default policy
    lock.acquire()
    _fail_close_of(mocker, lock, OSError(EIO, "close failed"))
    lock.release()  # Unix default drops a FUSE/Docker EIO
    assert not lock.is_locked


@_WINDOWS_ONLY
def test_close_error_default_raises_on_windows(tmp_path: Path, mocker: MockerFixture) -> None:
    lock = FileLock(str(tmp_path / "a"))  # default policy
    lock.acquire()
    _fail_close_of(mocker, lock, OSError(EIO, "close failed"))
    with pytest.raises(OSError, match="close failed"):
        lock.release()
    assert not lock.is_locked


def test_close_error_suppress(tmp_path: Path, mocker: MockerFixture) -> None:
    lock = FileLock(str(tmp_path / "a"), close_error_policy="suppress")
    lock.acquire()
    _fail_close_of(mocker, lock, OSError(EIO, "close failed"))
    lock.release()
    assert not lock.is_locked


@pytest.mark.parametrize("errno", [EINTR, EIO, ENOSPC])
def test_close_error_raise_is_exact_and_committed(tmp_path: Path, mocker: MockerFixture, errno: int) -> None:
    lock = FileLock(str(tmp_path / "a"), close_error_policy="raise")
    lock.acquire()
    injected = OSError(errno, "close failed")
    attempts = _fail_close_of(mocker, lock, injected)
    with pytest.raises(OSError, match="close failed") as info:
        lock.release()
    assert info.value is injected  # the original error, no wrapper
    assert len(attempts) == 1  # os.close is never retried, even after EINTR
    assert not lock.is_locked  # the unlock committed even though close failed
    assert lock.lock_counter == 0


@_UNIX_FLOCK_ONLY
def test_close_not_reached_when_unlock_fails(tmp_path: Path, mocker: MockerFixture) -> None:
    lock = FileLock(str(tmp_path / "a"), close_error_policy="raise")
    lock.acquire()
    fd = lock._context.lock_file_fd
    mocker.patch("filelock._unix.fcntl.flock", side_effect=[OSError(EIO, "unlock failed"), None])
    real_close = os.close
    closed: list[int] = []
    mocker.patch("filelock._api.os.close", side_effect=lambda target: closed.append(target) or real_close(target))
    with pytest.raises(OSError, match="unlock failed"):
        lock.release()
    assert lock.is_locked  # the kernel unlock failed, so the lock is still held
    assert fd not in closed  # close is not reached while the lock is still held
    lock.release()  # retry: unlock succeeds and close runs
    assert not lock.is_locked
    assert fd in closed


def test_dual_body_and_close_failure_grouped(tmp_path: Path, mocker: MockerFixture) -> None:
    lock = FileLock(str(tmp_path / "a"), close_error_policy="raise", context_error_policy="group")
    lock.acquire()
    _fail_close_of(mocker, lock, OSError("close failed"))
    body = ValueError("body failed")
    with pytest.raises(ExceptionGroup) as info:
        lock._release_in_context(body)  # what __exit__ runs; the close failure joins the body failure
    leaf_body, close = info.value.exceptions
    assert isinstance(leaf_body, ValueError)
    assert isinstance(close, OSError)


def test_invalid_close_error_policy_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="close_error_policy must be"):
        FileLock(str(tmp_path / "a"), close_error_policy="explode")  # ty: ignore[invalid-argument-type]


def test_singleton_rejects_different_close_policy(tmp_path: Path) -> None:
    path = str(tmp_path / "a")
    first = FileLock(path, is_singleton=True, close_error_policy="raise")
    try:
        with pytest.raises(ValueError, match="close_error_policy"):
            FileLock(path, is_singleton=True, close_error_policy="suppress")
    finally:
        first.release(force=True)
