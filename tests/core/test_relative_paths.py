from __future__ import annotations

import errno
import os
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Final

import pytest
from capabilities import CAPABILITIES

from filelock import (
    BaseFileLock,
    FileLock,
    MarkerSoftFileLock,
    SoftFileLease,
    SoftFileLock,
    Timeout,
)
from tests.capability_marks import (
    NEEDS_FCNTL,
    NEEDS_PARENT_SYMLINK_COLLAPSE,
)

if TYPE_CHECKING:
    from collections.abc import Callable


_NEEDS_O_NOFOLLOW: Final[pytest.MarkDecorator] = pytest.mark.skipif(
    not CAPABILITIES["o-nofollow"], reason="refusing to open through a final symlink needs a honored O_NOFOLLOW"
)

_RELATIVE_LOCK_TYPES: Final[list[type[BaseFileLock]]] = [FileLock, SoftFileLock, MarkerSoftFileLock, SoftFileLease]


@pytest.mark.parametrize("lock_type", _RELATIVE_LOCK_TYPES)
def test_relative_waiter_keeps_acquisition_directory(
    lock_type: type[BaseFileLock], working_directories: tuple[Path, Path]
) -> None:
    # The waiter must keep its original path if another thread changes directories.
    first, second = working_directories
    holder = lock_type(first / "resource.lock")
    holder.acquire()
    polling = threading.Event()
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(_acquire_relative_lock, lock_type, polling)
            assert polling.wait(timeout=2)
            os.chdir(second)
            with pytest.raises(Timeout):
                future.result(timeout=2)
    finally:
        holder.release(force=True)

    assert not (second / "resource.lock").exists()


def _acquire_relative_lock(lock_type: type[BaseFileLock], polling: threading.Event, timeout: float = 0.2) -> None:
    lock_type("resource.lock", timeout=timeout, poll_interval=0.001).acquire(
        cancel_check=partial(_signal_poll, polling)
    )


def _signal_poll(polling: threading.Event) -> bool:
    polling.set()
    return False


@pytest.mark.parametrize("lock_type", [SoftFileLock, MarkerSoftFileLock, SoftFileLease])
def test_relative_release_uses_acquisition_directory(
    lock_type: type[BaseFileLock], working_directories: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    first, second = working_directories
    lock = lock_type("resource.lock")
    lock.acquire()

    monkeypatch.chdir(second)
    with lock:
        pass
    lock.release()

    assert not (first / "resource.lock").exists()


@pytest.mark.skipif(
    sys.platform == "win32", reason="Windows refuses to rename a directory holding an open file"
)  # pragma: win32 no cover
@pytest.mark.parametrize("lock_type", [SoftFileLock, MarkerSoftFileLock, SoftFileLease])
def test_relative_release_after_directory_rename_removes_marker(
    lock_type: type[BaseFileLock], working_directories: tuple[Path, Path]
) -> None:
    first, _ = working_directories
    lock = lock_type("resource.lock")
    lock.acquire()
    renamed = first.parent / "renamed"
    Path(first).rename(renamed)

    lock.release()

    assert not (renamed / "resource.lock").exists()


@pytest.mark.skipif(
    sys.platform == "win32", reason="Windows refuses to rename a directory holding an open file"
)  # pragma: win32 no cover
@pytest.mark.parametrize("lock_type", [FileLock, SoftFileLock])
def test_waiter_after_directory_rename_does_not_lock_a_new_file(
    lock_type: type[BaseFileLock], working_directories: tuple[Path, Path]
) -> None:
    first, _ = working_directories
    holder = lock_type(first / "resource.lock")
    holder.acquire()
    polling = threading.Event()
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(_acquire_relative_lock, lock_type, polling, 2)
            assert polling.wait(timeout=2)
            Path(first).rename(first.parent / "renamed")
            with pytest.raises(FileNotFoundError):
                future.result(timeout=5)
        assert not (first / "resource.lock").exists()
        assert holder.is_locked
    finally:
        holder.release(force=True)


@pytest.mark.skipif(
    sys.platform == "win32", reason="Windows refuses to rename a directory holding an open file"
)  # pragma: win32 no cover
@pytest.mark.parametrize("lock_type", [FileLock, SoftFileLock])
@pytest.mark.parametrize(
    "occupy_old_name",
    [
        pytest.param(lambda _parent: None, id="old-name-absent"),
        pytest.param(lambda parent: parent.write_text("replacement", encoding="utf-8"), id="old-name-is-a-file"),
    ],
)
def test_reentrant_acquire_after_directory_rename(
    lock_type: type[BaseFileLock], occupy_old_name: Callable[[Path], object], tmp_path: Path
) -> None:
    parent = tmp_path / "original"
    lock = lock_type(parent / "resource.lock")
    with lock:
        parent.rename(tmp_path / "renamed")
        occupy_old_name(parent)
        with lock:
            assert lock.lock_counter == 2
        assert lock.lock_counter == 1
        assert not parent.is_dir()
    assert not lock.is_locked


@pytest.mark.parametrize("lock_type", _RELATIVE_LOCK_TYPES)
def test_each_acquisition_resolves_the_working_directory(
    lock_type: type[BaseFileLock], working_directories: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    _, second = working_directories
    lock = lock_type("resource.lock")
    with lock:
        pass

    monkeypatch.chdir(second)
    with lock:
        assert (lock.lock_file, (second / "resource.lock").exists()) == ("resource.lock", True)


@pytest.mark.skipif(
    sys.platform == "win32", reason="a working directory in use cannot be removed"
)  # pragma: win32 no cover
@pytest.mark.parametrize("lock_type", _RELATIVE_LOCK_TYPES)
def test_acquire_without_working_directory_leaves_no_hold(
    lock_type: type[BaseFileLock], working_directories: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    lock: Final[BaseFileLock] = lock_type("resource.lock")
    working_directories[0].rmdir()

    with pytest.raises(FileNotFoundError):
        lock.acquire()
    second: Final[Path] = working_directories[1]
    monkeypatch.chdir(second)
    with lock:
        assert (lock.lock_counter, lock.is_locked, (second / "resource.lock").exists()) == (1, True, True)

    assert (lock.lock_counter, lock.is_locked) == (0, False)


@pytest.mark.skipif(
    sys.platform == "win32", reason="a working directory in use cannot be removed"
)  # pragma: win32 no cover
@pytest.mark.parametrize("lock_type", _RELATIVE_LOCK_TYPES)
def test_nested_acquire_without_working_directory_preserves_hold(
    lock_type: type[BaseFileLock], working_directories: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    lock: Final[BaseFileLock] = lock_type("resource.lock")
    lock.acquire()
    first: Final[Path] = working_directories[0]
    (first / "resource.lock").unlink()
    first.rmdir()

    with pytest.raises(FileNotFoundError):
        lock.acquire()
    assert (lock.lock_counter, lock.is_locked) == (1, True)

    monkeypatch.chdir(working_directories[1])
    lock.release()
    assert (lock.lock_counter, lock.is_locked) == (0, False)


@pytest.mark.parametrize("lock_type", _RELATIVE_LOCK_TYPES)
def test_singleton_after_chdir_locks_the_requested_file(
    lock_type: type[BaseFileLock], working_directories: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    first, second = working_directories
    relative = lock_type("resource.lock", is_singleton=True)
    monkeypatch.chdir(second)
    with lock_type(first / "resource.lock", is_singleton=True):
        assert ((first / "resource.lock").exists(), (second / "resource.lock").exists()) == (True, False)
    del relative


@pytest.mark.parametrize("lock_type", _RELATIVE_LOCK_TYPES)
def test_shared_singleton_refuses_moved_relative_path(
    lock_type: type[BaseFileLock], working_directories: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    first, second = working_directories
    relative = lock_type("resource.lock", is_singleton=True)
    absolute = lock_type(first / "resource.lock", is_singleton=True)
    assert absolute is relative

    monkeypatch.chdir(second)
    with pytest.raises(RuntimeError, match="singleton lock path changed after sharing"):
        absolute.acquire(timeout=0)

    assert not (second / "resource.lock").exists()
    with lock_type(first / "resource.lock", is_singleton=True):
        assert (first / "resource.lock").exists()


@pytest.mark.parametrize("lock_type", _RELATIVE_LOCK_TYPES)
def test_unshared_singleton_follows_chdir(
    lock_type: type[BaseFileLock], working_directories: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    first, second = working_directories
    lock = lock_type("resource.lock", is_singleton=True)
    monkeypatch.chdir(second)

    with lock:
        assert (second / "resource.lock").exists()
    assert not (first / "resource.lock").exists()


def test_moved_shared_singleton_still_reports_owner(
    working_directories: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    first, second = working_directories
    lock = MarkerSoftFileLock("resource.lock", is_singleton=True)
    assert MarkerSoftFileLock(first / "resource.lock", is_singleton=True) is lock
    monkeypatch.chdir(second)

    assert (lock.pid, lock.is_lock_held_by_us, lock.owner) == (None, False, None)


def test_equivalent_relative_singleton_spellings_follow_chdir(
    working_directories: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    first, second = working_directories
    plain = FileLock("resource.lock", is_singleton=True)
    dotted = FileLock("./resource.lock", is_singleton=True)
    assert dotted is plain

    monkeypatch.chdir(second)
    with dotted:
        assert (second / "resource.lock").exists()
    assert not (first / "resource.lock").exists()


@pytest.mark.parametrize("lock_type", _RELATIVE_LOCK_TYPES)
def test_singleton_held_before_chdir_re_enters_by_absolute_path(
    lock_type: type[BaseFileLock], working_directories: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    first, second = working_directories
    held = lock_type("resource.lock", is_singleton=True)
    held.acquire()
    monkeypatch.chdir(second)
    with lock_type(first / "resource.lock", is_singleton=True) as again:
        assert (again is held, held.lock_counter) == (True, 2)
    held.release(force=True)


@NEEDS_PARENT_SYMLINK_COLLAPSE  # pragma: win32 no cover
@pytest.mark.parametrize("lock_type", _RELATIVE_LOCK_TYPES)
def test_singleton_after_parent_link_moves_locks_the_requested_file(
    lock_type: type[BaseFileLock], working_directories: tuple[Path, Path]
) -> None:
    first, second = working_directories
    link = first.parent / "current"
    link.symlink_to(first)
    through_link = lock_type(link / "resource.lock", is_singleton=True)
    link.unlink()
    link.symlink_to(second)
    with lock_type(first / "resource.lock", is_singleton=True):
        assert ((first / "resource.lock").exists(), (second / "resource.lock").exists()) == (True, False)
    del through_link


@NEEDS_FCNTL  # pragma: needs fcntl
@_NEEDS_O_NOFOLLOW  # pragma: needs o-nofollow
def test_final_symlink_backend_refuses_to_lock(tmp_path: Path) -> None:
    (tmp_path / "target").write_text("")
    (tmp_path / "link").symlink_to(tmp_path / "target")
    # O_NOFOLLOW reports ELOOP on Linux and macOS, EFTYPE on NetBSD, and EMLINK on FreeBSD.
    with pytest.raises(
        OSError, check=lambda error: error.errno in {errno.ELOOP, errno.EMLINK, getattr(errno, "EFTYPE", None)}
    ):
        FileLock(str(tmp_path / "link")).acquire()
