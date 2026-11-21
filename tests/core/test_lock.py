from __future__ import annotations

import errno
import logging
import os
import runpy
import sys
import textwrap
from contextlib import contextmanager
from pathlib import Path, PurePath
from stat import S_IWGRP, S_IWOTH, S_IWUSR
from typing import TYPE_CHECKING, Final, cast

import pytest

from filelock import (
    BaseFileLock,
    FileLock,
    SoftFileLock,
)
from tests.capability_marks import (
    NEEDS_FILE_PERMISSIONS,
    WINDOWS_ONLY,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Generator, Iterator


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

    # A runtime with a lazy collector may finalize, mid-test, a lock an earlier test dropped.
    assert [(r.name, r.levelno, r.getMessage()) for r in caplog.records if str(lock_path) in r.getMessage()] == [
        ("filelock", logging.DEBUG, f"Attempting to acquire lock {id(lock)} on {lock_path}"),
        ("filelock", logging.DEBUG, f"Lock {id(lock)} acquired on {lock_path}"),
        ("filelock", logging.DEBUG, f"Attempting to release lock {id(lock)} on {lock_path}"),
        ("filelock", logging.DEBUG, f"Lock {id(lock)} released on {lock_path}"),
    ]
    assert logging.getLogger("filelock").level == logging.NOTSET


@pytest.mark.parametrize(
    "key", [pytest.param("/short-key", id="absolute"), pytest.param("\udcff", id="undecodable-filename")]
)
def test_documented_cache_lock_name(tmp_path: Path, key: str) -> None:
    source: Final[str] = (Path(__file__).parents[2] / "docs" / "how-to.rst").read_text(encoding="utf-8")
    snippet: Final[str] = source.split(".. code-block:: python\n", 1)[1].split("\n``datasets``", 1)[0]
    recipe: Final[Path] = tmp_path / "cache_recipe.py"
    recipe.write_text(textwrap.dedent(snippet), encoding="utf-8")
    lock_for = cast("Callable[[str, str], FileLock]", runpy.run_path(str(recipe))["lock_for"])
    cache_dir: Final[Path] = tmp_path / "cache"
    with (lock := lock_for(str(cache_dir), key)):
        lock_path: Final[Path] = Path(lock.lock_file)
        assert (lock_path.parent, len(lock_path.name), lock_path.name.isascii()) == (cache_dir, 69, True)


@pytest.fixture
def tmp_path_ro(tmp_path: Path) -> Iterator[Path]:  # pragma: needs file-permissions
    with make_ro(tmp_path):
        yield tmp_path


@contextmanager
def make_ro(path: Path) -> Generator[None]:
    write = S_IWUSR | S_IWGRP | S_IWOTH
    path.chmod(path.stat().st_mode & ~write)
    try:
        yield
    finally:
        path.chmod(path.stat().st_mode | write)


@pytest.mark.parametrize("lock_type", [FileLock, SoftFileLock])
@NEEDS_FILE_PERMISSIONS  # pragma: needs file-permissions
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


@pytest.mark.parametrize("lock_type", [FileLock, SoftFileLock])
@pytest.mark.parametrize(
    ("expected_error", "expected_errno", "bad_lock_file"),
    [
        # Win32 error messages depend on the system locale.
        pytest.param(
            OSError,
            (errno.ENOENT, errno.EINVAL, errno.EACCES),
            "",
            id="blank_filename",
        ),
        pytest.param(
            PermissionError,
            (errno.EACCES,),
            ".",
            id="current_directory",
            marks=pytest.mark.skipif(sys.platform != "win32", reason="Windows reports a directory open as EACCES"),
        ),
        pytest.param(
            IsADirectoryError,
            (errno.EISDIR,),
            ".",
            id="current_directory-posix",
            marks=pytest.mark.skipif(sys.platform not in {"darwin", "linux"}, reason="POSIX reports EISDIR"),
        ),
        pytest.param(
            OSError,
            None,
            ".",
            id="current_directory-other",
            marks=pytest.mark.skipif(
                sys.platform in {"darwin", "linux", "win32"}, reason="other platforms report no pinned errno"
            ),
        ),
    ]
    + [pytest.param(OSError, (errno.EINVAL,), i, id=f"invalid_{i}", marks=WINDOWS_ONLY) for i in '<>:"|?*\a']
    + [pytest.param(PermissionError, (errno.EACCES,), i, id=f"permission_{i}", marks=WINDOWS_ONLY) for i in "/\\"],
)
@pytest.mark.timeout(5)
def test_bad_lock_file(
    lock_type: type[BaseFileLock],
    expected_error: type[OSError],
    expected_errno: tuple[int, ...] | None,
    bad_lock_file: str,
) -> None:
    lock: Final[BaseFileLock] = lock_type(bad_lock_file)

    with pytest.raises(expected_error) as raised:
        lock.acquire()
    assert expected_errno is None or raised.value.errno in expected_errno


@pytest.mark.parametrize("lock_type", [FileLock, SoftFileLock])
@pytest.mark.timeout(5)
def test_bad_lock_file_null_byte(lock_type: type[BaseFileLock]) -> None:
    lock: Final[BaseFileLock] = lock_type("\0")

    with pytest.raises(ValueError, match=r"embedded null (byte|character)"):
        lock.acquire()


@pytest.mark.parametrize("lock_type", [FileLock, SoftFileLock])
def test_nested_context_manager(lock_type: type[BaseFileLock], tmp_path: Path) -> None:
    lock = lock_type(str(tmp_path / "a"))

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
    lock = lock_type(str(tmp_path / "a"))

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
    lock = lock_type(str(tmp_path / "a"))

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


@pytest.mark.parametrize("lock_type", [FileLock, SoftFileLock])
def test_context_release_on_exc(lock_type: type[BaseFileLock], tmp_path: Path) -> None:
    lock = lock_type(str(tmp_path / "a"))

    try:
        with lock as lock_1:
            assert lock is lock_1
            assert lock.is_locked
            raise ValueError  # ruff:ignore[raise-within-try]  # raises inside the block to assert release on exception
    except ValueError:
        assert not lock.is_locked


@pytest.mark.parametrize("lock_type", [FileLock, SoftFileLock])
def test_acquire_release_on_exc(lock_type: type[BaseFileLock], tmp_path: Path) -> None:
    lock = lock_type(str(tmp_path / "a"))

    try:
        with lock.acquire() as lock_1:
            assert lock is lock_1
            assert lock.is_locked
            raise ValueError  # ruff:ignore[raise-within-try]  # raises inside the block to assert release on exception
    except ValueError:
        assert not lock.is_locked


@pytest.mark.parametrize("lock_type", [FileLock, SoftFileLock])
def test_context_decorator(lock_type: type[BaseFileLock], tmp_path: Path) -> None:
    lock = lock_type(str(tmp_path / "a"))

    @lock
    def decorated_method() -> None:
        assert lock.is_locked

    assert not lock.is_locked
    decorated_method()
    assert not lock.is_locked


def test_file_lock_positional_argument(tmp_path: Path) -> None:
    class FilePathLock(FileLock):
        def __init__(self, file_path: str) -> None:
            super().__init__(f"{file_path}.lock")

    lock_path = tmp_path / "a"
    lock = FilePathLock(str(lock_path))
    assert lock.lock_file == f"{lock_path!s}.lock"


def test_acquire_proxy_enters_as_the_lock_type(tmp_path: Path) -> None:
    lock: Final[SoftFileLock] = FileLock(tmp_path / "a.lock")
    # This method exists only on the concrete lock returned by the proxy.
    with lock.acquire() as held:
        assert (held, held.lock_counter) == (lock, 1)
