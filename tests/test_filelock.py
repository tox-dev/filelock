from __future__ import annotations

import inspect
import logging
import os
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from errno import ENOSYS
from inspect import getframeinfo, stack
from pathlib import Path, PurePath
from stat import S_IWGRP, S_IWOTH, S_IWUSR, filemode
from types import TracebackType
from typing import TYPE_CHECKING, Any, Callable, Iterator, Tuple, Type, Union
from uuid import uuid4
from weakref import WeakValueDictionary

import pytest

from filelock import BaseFileLock, FileLock, SoftFileLock, Timeout, UnixFileLock, WindowsFileLock

if TYPE_CHECKING:
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

    # test lock creation by passing a `str`
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
    yield
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


WindowsOnly = pytest.mark.skipif(sys.platform != "win32", reason="Windows only")


@pytest.mark.parametrize("lock_type", [FileLock, SoftFileLock])
@pytest.mark.parametrize(
    ("expected_error", "match", "bad_lock_file"),
    [
        pytest.param(FileNotFoundError, "No such file or directory:", "", id="blank_filename"),
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
    + [pytest.param(OSError, "Invalid argument", i, id=f"invalid_{i}", marks=WindowsOnly) for i in '<>:"|?*\a']
    + [pytest.param(PermissionError, "Permission denied:", i, id=f"permission_{i}", marks=WindowsOnly) for i in "/\\"],
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
    # lock is not released before the most outer with statement that locked the lock, is left
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
    # lock is not released before the most outer with statement that locked the lock, is left
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
    # acquires the lock using a with-statement and releases the lock before leaving the with-statement
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
    # lock is re-entrant for a given file even if it is constructed multiple times
    lock_path = tmp_path / "a"

    with lock_type(str(lock_path), is_singleton=True, timeout=2) as lock_1:
        assert lock_1.is_locked

        with lock_type(str(lock_path), is_singleton=True, timeout=2) as lock_2:
            assert lock_2 is lock_1
            assert lock_2.is_locked

        assert lock_1.is_locked

    assert not lock_1.is_locked


_ExcInfoType = Union[Tuple[Type[BaseException], BaseException, TracebackType], Tuple[None, None, None]]


class ExThread(threading.Thread):
    def __init__(self, target: Callable[[], None], name: str) -> None:
        super().__init__(target=target, name=name)
        self.ex: _ExcInfoType | None = None

    def run(self) -> None:
        try:
            super().run()
        except Exception:  # noqa: BLE001 # pragma: no cover
            self.ex = sys.exc_info()  # pragma: no cover

    def join(self, timeout: float | None = None) -> None:
        super().join(timeout=timeout)
        if self.ex is not None:
            raise RuntimeError from self.ex[1]  # pragma: no cover


@pytest.mark.parametrize("lock_type", [FileLock, SoftFileLock])
def test_threaded_shared_lock_obj(lock_type: type[BaseFileLock], tmp_path: Path) -> None:
    # Runs 100 threads, which need the filelock. The lock must be acquired if at least one thread required it and
    # released, as soon as all threads stopped.
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
@pytest.mark.skipif(hasattr(sys, "pypy_version_info") and sys.platform == "win32", reason="deadlocks randomly")
def test_threaded_lock_different_lock_obj(lock_type: type[BaseFileLock], tmp_path: Path) -> None:
    # Runs multiple threads, which acquire the same lock file with a different FileLock object. When thread group 1
    # acquired the lock, thread group 2 must not hold their lock.

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
    # raises Timeout error when the lock cannot be acquired
    lock_path = tmp_path / "a"
    lock_1, lock_2 = lock_type(str(lock_path)), lock_type(str(lock_path))

    # acquire lock 1
    lock_1.acquire()
    assert lock_1.is_locked
    assert not lock_2.is_locked

    # try to acquire lock 2
    with pytest.raises(Timeout, match="The file lock '.*' could not be acquired."):
        lock_2.acquire(timeout=0.1)
    assert not lock_2.is_locked
    assert lock_1.is_locked

    # release lock 1
    lock_1.release()
    assert not lock_1.is_locked
    assert not lock_2.is_locked


@pytest.mark.parametrize("lock_type", [FileLock, SoftFileLock])
def test_non_blocking(lock_type: type[BaseFileLock], tmp_path: Path) -> None:
    # raises Timeout error when the lock cannot be acquired
    lock_path = tmp_path / "a"
    lock_1, lock_2 = lock_type(str(lock_path)), lock_type(str(lock_path))
    lock_3 = lock_type(str(lock_path), blocking=False)
    lock_4 = lock_type(str(lock_path), timeout=0)
    lock_5 = lock_type(str(lock_path), blocking=False, timeout=-1)

    # acquire lock 1
    lock_1.acquire()
    assert lock_1.is_locked
    assert not lock_2.is_locked
    assert not lock_3.is_locked
    assert not lock_4.is_locked
    assert not lock_5.is_locked

    # try to acquire lock 2
    with pytest.raises(Timeout, match="The file lock '.*' could not be acquired."):
        lock_2.acquire(blocking=False)
    assert not lock_2.is_locked
    assert lock_1.is_locked

    # try to acquire pre-parametrized `blocking=False` lock 3 with `acquire`
    with pytest.raises(Timeout, match="The file lock '.*' could not be acquired."):
        lock_3.acquire()
    assert not lock_3.is_locked
    assert lock_1.is_locked

    # try to acquire pre-parametrized `blocking=False` lock 3 with context manager
    with pytest.raises(Timeout, match="The file lock '.*' could not be acquired."), lock_3:
        pass
    assert not lock_3.is_locked
    assert lock_1.is_locked

    # try to acquire pre-parametrized `timeout=0` lock 4 with `acquire`
    with pytest.raises(Timeout, match="The file lock '.*' could not be acquired."):
        lock_4.acquire()
    assert not lock_4.is_locked
    assert lock_1.is_locked

    # try to acquire pre-parametrized `timeout=0` lock 4 with context manager
    with pytest.raises(Timeout, match="The file lock '.*' could not be acquired."), lock_4:
        pass
    assert not lock_4.is_locked
    assert lock_1.is_locked

    # blocking precedence over timeout
    # try to acquire pre-parametrized `timeout=-1,blocking=False` lock 5 with `acquire`
    with pytest.raises(Timeout, match="The file lock '.*' could not be acquired."):
        lock_5.acquire()
    assert not lock_5.is_locked
    assert lock_1.is_locked

    # try to acquire pre-parametrized `timeout=-1,blocking=False` lock 5 with context manager
    with pytest.raises(Timeout, match="The file lock '.*' could not be acquired."), lock_5:
        pass
    assert not lock_5.is_locked
    assert lock_1.is_locked

    # release lock 1
    lock_1.release()
    assert not lock_1.is_locked
    assert not lock_2.is_locked
    assert not lock_3.is_locked
    assert not lock_4.is_locked
    assert not lock_5.is_locked


@pytest.mark.parametrize("lock_type", [FileLock, SoftFileLock])
def test_default_timeout(lock_type: type[BaseFileLock], tmp_path: Path) -> None:
    # test if the default timeout parameter works
    lock_path = tmp_path / "a"
    lock_1, lock_2 = lock_type(str(lock_path)), lock_type(str(lock_path), timeout=0.1)
    assert lock_2.timeout == 0.1

    # acquire lock 1
    lock_1.acquire()
    assert lock_1.is_locked
    assert not lock_2.is_locked

    # try to acquire lock 2
    with pytest.raises(Timeout, match="The file lock '.*' could not be acquired."):
        lock_2.acquire()
    assert not lock_2.is_locked
    assert lock_1.is_locked

    lock_2.timeout = 0
    assert lock_2.timeout == 0

    with pytest.raises(Timeout, match="The file lock '.*' could not be acquired."):
        lock_2.acquire()
    assert not lock_2.is_locked
    assert lock_1.is_locked

    # release lock 1
    lock_1.release()
    assert not lock_1.is_locked
    assert not lock_2.is_locked


@pytest.mark.parametrize("lock_type", [FileLock, SoftFileLock])
def test_context_release_on_exc(lock_type: type[BaseFileLock], tmp_path: Path) -> None:
    # lock is released when an exception is thrown in a with-statement
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
    # lock is released when an exception is thrown in a acquire statement
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
    # lock is released when the object is deleted
    lock_path = tmp_path / "a"
    lock_1, lock_2 = lock_type(str(lock_path)), lock_type(str(lock_path))

    # acquire lock 1
    lock_1.acquire()
    assert lock_1.is_locked
    assert not lock_2.is_locked

    # try to acquire lock 2
    with pytest.raises(Timeout, match="The file lock '.*' could not be acquired."):
        lock_2.acquire(timeout=0.1)

    # delete lock 1 and try to acquire lock 2 again
    del lock_1

    lock_2.acquire()
    assert lock_2.is_locked

    lock_2.release()


def test_cleanup_soft_lock(tmp_path: Path) -> None:
    # tests if the lock file is removed after use
    lock_path = tmp_path / "a"

    with SoftFileLock(lock_path):
        assert lock_path.exists()
    assert not lock_path.exists()


@pytest.mark.parametrize("lock_type", [FileLock, SoftFileLock])
def test_poll_intervall_deprecated(lock_type: type[BaseFileLock], tmp_path: Path) -> None:
    lock_path = tmp_path / "a"
    lock = lock_type(str(lock_path))

    with pytest.deprecated_call(match="use poll_interval instead of poll_intervall") as checker:
        lock.acquire(poll_intervall=0.05)  # the deprecation warning will be captured by the checker
        frame_info = getframeinfo(stack()[0][0])  # get frame info of current file and lineno (+1 than the above lineno)
        for warning in checker:
            if warning.filename == frame_info.filename and warning.lineno + 1 == frame_info.lineno:  # pragma: no cover
                break
        else:  # pragma: no cover
            pytest.fail("No warnings of stacklevel=2 matching.")


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
    # test file lock permissions are independent of umask
    lock_path = tmp_path / "a.lock"
    lock = FileLock(str(lock_path), mode=0o666)

    # set umask so permissions can be anticipated
    initial_umask = os.umask(0o022)
    try:
        lock.acquire()
        assert lock.is_locked

        mode = filemode(lock_path.stat().st_mode)
        assert mode == "-rw-rw-rw-"
    finally:
        os.umask(initial_umask)

    lock.release()


def test_lock_mode_soft(tmp_path: Path) -> None:
    # test soft lock permissions are dependent of umask
    lock_path = tmp_path / "a.lock"
    lock = SoftFileLock(str(lock_path), mode=0o666)

    # set umask so permissions can be anticipated
    initial_umask = os.umask(0o022)
    try:
        lock.acquire()
        assert lock.is_locked

        mode = filemode(lock_path.stat().st_mode)
        if sys.platform == "win32":
            assert mode == "-rw-rw-rw-"
        else:
            assert mode == "-rw-r--r--"
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
        lock._release()  # noqa: SLF001


@pytest.mark.skipif(sys.platform == "win32", reason="flock not run on windows")
def test_flock_not_implemented_unix(tmp_path: Path, mocker: MockerFixture) -> None:
    mocker.patch("fcntl.flock", side_effect=OSError(ENOSYS, "mock error"))
    with pytest.raises(NotImplementedError), FileLock(tmp_path / "a.lock"):
        pass


def test_soft_errors(tmp_path: Path, mocker: MockerFixture) -> None:
    mocker.patch("os.open", side_effect=OSError(ENOSYS, "mock error"))
    with pytest.raises(OSError, match="mock error"):
        SoftFileLock(tmp_path / "a.lock").acquire()


def _check_file_read_write(txt_file: Path) -> None:
    for _ in range(3):
        uuid = str(uuid4())
        txt_file.write_text(uuid)
        assert txt_file.read_text() == uuid


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
            super().__init__(lock_file, timeout, mode, thread_local, blocking=True, is_singleton=True)
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
    args: dict[str, Any] = {"timeout": -1, "mode": 0o644, "thread_local": True, "blocking": True}
    alternate_args: dict[str, Any] = {"timeout": 10, "mode": 0, "thread_local": False, "blocking": False}

    lock = lock_type(str(lock_path), is_singleton=True, **args)

    for arg_name in args:
        general_msg = "Singleton lock instances cannot be initialized with differing arguments"
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

    assert lock_type._instances == {str(lock_path): lock}  # noqa: SLF001
    del lock
    assert lock_type._instances == {}  # noqa: SLF001


@pytest.mark.skipif(hasattr(sys, "pypy_version_info"), reason="del() does not trigger GC in PyPy")
@pytest.mark.parametrize("lock_type", [FileLock, SoftFileLock])
def test_singleton_instance_tracking_is_unique_per_subclass(lock_type: type[BaseFileLock]) -> None:
    class Lock1(lock_type):  # type: ignore[valid-type, misc]
        pass

    class Lock2(lock_type):  # type: ignore[valid-type, misc]
        pass

    assert isinstance(Lock1._instances, WeakValueDictionary)  # noqa: SLF001
    assert isinstance(Lock2._instances, WeakValueDictionary)  # noqa: SLF001
    assert Lock1._instances is not Lock2._instances  # noqa: SLF001


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
