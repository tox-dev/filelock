from __future__ import annotations

import logging
import os
import subprocess  # ruff:ignore[suspicious-subprocess-import]  # only fixed-argument, no-shell runs below
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, suppress
from typing import TYPE_CHECKING, Final

import pytest

from filelock import (
    BaseFileLock,
    FileLock,
    SoftFileLock,
    StrictSoftFileLock,
    Timeout,
)
from tests.capability_marks import (
    NEEDS_PROMPT_FINALIZATION,
)
from tests.core.thread_helpers import THREAD_WAIT

if TYPE_CHECKING:
    from collections.abc import Callable, Generator
    from pathlib import Path
    from types import FrameType

    from _typeshed import Unused  # simplint: ignore[private-import-path]  # precise callback type
    from pytest_mock import MockerFixture


@NEEDS_PROMPT_FINALIZATION
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


@pytest.mark.parametrize(
    "lock_type",
    [
        pytest.param(FileLock, id="native"),
        pytest.param(SoftFileLock, id="soft"),
        pytest.param(StrictSoftFileLock, marks=pytest.mark.requires_hard_links, id="strict"),
    ],
)
def test_lock_held_at_exit_is_released_silently(lock_type: type[BaseFileLock], tmp_path: Path) -> None:
    # Module teardown clears the globals needed to release a live lock.
    lock_path = tmp_path / "a.lock"
    script = f"import sys, filelock; sys.held = filelock.{lock_type.__name__}({str(lock_path)!r}); sys.held.acquire()"
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, check=True)

    assert not result.stderr
    with lock_type(lock_path).acquire(timeout=0):
        pass


@pytest.mark.parametrize("lock_type", [FileLock, SoftFileLock])
@pytest.mark.parametrize("thread_local", [pytest.param(True, id="thread-local"), pytest.param(False, id="shared")])
def test_lock_a_daemon_thread_holds_at_exit_stays_held(
    lock_type: type[BaseFileLock], tmp_path: Path, *, thread_local: bool
) -> None:
    # Exit handlers run while daemon threads still do, so releasing their hold would let another process in mid-section.
    lock_path = str(tmp_path / "a.lock")
    contender = f"import filelock; filelock.{lock_type.__name__}({lock_path!r}).acquire(timeout=0)"
    script = f"""
import atexit, subprocess, sys, threading
atexit.register(lambda: print(subprocess.run([sys.executable, "-c", {contender!r}], capture_output=True).returncode))
import filelock
lock = filelock.{lock_type.__name__}({lock_path!r}, thread_local={thread_local})
entered = threading.Event()
def hold():
    with lock:
        entered.set()
        threading.Event().wait()
threading.Thread(target=hold, daemon=True).start()
entered.wait()
"""
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, check=True)

    assert (result.stdout, result.stderr) == ("1\n", "")


@pytest.mark.parametrize("lock_type", [FileLock, SoftFileLock])
def test_force_release_keeps_main_thread_atexit_hold(lock_type: type[BaseFileLock], tmp_path: Path) -> None:
    lock_path = tmp_path / "a.lock"
    script = f"""
import atexit, sys, threading
from filelock import {lock_type.__name__}
lock = {lock_type.__name__}({str(lock_path)!r})
release_now = threading.Event()
released = threading.Event()
def force_release():
    release_now.wait()
    lock.release(force=True)
    released.set()
threading.Thread(target=force_release, daemon=True).start()
def save():
    with lock:
        release_now.set()
        print(released.wait(2), lock.is_locked, flush=True)
        sys.stdin.readline()
atexit.register(save)
"""
    process = subprocess.Popen(
        [sys.executable, "-c", script], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    )
    try:
        assert process.stdout is not None
        assert process.stdout.readline() == "True True\n"
        with pytest.raises(Timeout):
            lock_type(lock_path, blocking=False).acquire()
    finally:
        stdout, stderr = process.communicate("done\n", timeout=3)
    assert (process.returncode, stdout, stderr) == (0, "", "")


@pytest.mark.parametrize("lock_type", [FileLock, SoftFileLock])
def test_force_release_keeps_main_thread_hold_during_threading_shutdown(
    lock_type: type[BaseFileLock], tmp_path: Path
) -> None:
    lock_path = tmp_path / "a.lock"
    script = f"""
import atexit, sys, threading, time
from filelock import {lock_type.__name__}, Timeout
lock = {lock_type.__name__}({str(lock_path)!r})
lock.acquire()
main = threading.main_thread()
def release_after_main():
    while main.is_alive():
        time.sleep(0.001)
    lock.release(force=True)
    try:
        with {lock_type.__name__}({str(lock_path)!r}, blocking=False):
            print("contender entered", flush=True)
    except Timeout:
        print("held", flush=True)
threading.Thread(target=release_after_main).start()
def save():
    print(lock.is_locked, flush=True)
    lock.release()
atexit.register(save)
"""
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=5, check=True)

    assert (result.stdout, result.stderr) == ("held\nTrue\n", "")


@pytest.mark.parametrize("lock_type", [FileLock, SoftFileLock])
@pytest.mark.parametrize(
    "drop",
    [
        pytest.param(list.clear, id="del", marks=NEEDS_PROMPT_FINALIZATION),
        pytest.param(lambda box: box[0].release(force=True), id="force-release"),
    ],
)
def test_releases_hold_of_exited_thread(
    lock_type: type[BaseFileLock], drop: Callable[[list[BaseFileLock]], None], tmp_path: Path
) -> None:
    lock_path: Final[str] = str(tmp_path / "a")
    box: Final[list[BaseFileLock]] = [lock_type(lock_path)]
    _acquire_in_exited_thread(box[0])
    drop(box)
    with lock_type(lock_path, blocking=False) as other:
        assert other.is_locked


def _acquire_in_exited_thread(lock: BaseFileLock) -> None:
    worker: Final[threading.Thread] = threading.Thread(target=lock.acquire)
    worker.start()
    worker.join()


@NEEDS_PROMPT_FINALIZATION
@pytest.mark.parametrize("lock_type", [FileLock, SoftFileLock])
def test_del_releases_hold_of_live_thread(lock_type: type[BaseFileLock], tmp_path: Path) -> None:
    lock_path: Final[str] = str(tmp_path / "a")
    lock = lock_type(lock_path)
    with _held_by_live_thread([lock]):
        del lock
        with lock_type(lock_path, blocking=False) as other:
            assert other.is_locked


@contextmanager
def _held_by_live_thread(box: list[BaseFileLock]) -> Generator[None]:
    acquired, done = threading.Event(), threading.Event()

    def hold() -> None:
        # Popping leaves the worker without a reference, so only the test's own name keeps the lock alive.
        box.pop().acquire()
        acquired.set()
        done.wait()

    worker: Final[threading.Thread] = threading.Thread(target=hold)
    worker.start()
    acquired.wait()
    try:
        yield
    finally:
        done.set()
        worker.join()


@NEEDS_PROMPT_FINALIZATION
@pytest.mark.parametrize("thread_local", [pytest.param(True, id="thread_local"), pytest.param(False, id="shared")])
def test_singleton_handed_out_while_finalizing_keeps_its_hold(tmp_path: Path, thread_local: bool) -> None:
    lock_path: Final[str] = str(tmp_path / "a")
    handed_out: Final[SoftFileLock] = _singleton_handed_out_while_finalizing(lock_path, thread_local=thread_local)
    with pytest.raises(Timeout):
        FileLock(lock_path, blocking=False).acquire()
    handed_out.release()


def _singleton_handed_out_while_finalizing(lock_path: str, *, thread_local: bool) -> FileLock:
    # The cache still finds a lock whose finalizer is starting, as another thread asking at that moment would.
    handed_out: Final[list[FileLock]] = []

    # Interpreters run a profile hook with tracing off, so coverage does not record its body.
    def take_from_cache(frame: FrameType, event: str, _arg: Unused) -> None:  # pragma: no cover
        if event == "call" and frame.f_code is BaseFileLock.__del__.__code__ and not handed_out:
            handed_out.append(FileLock(lock_path, is_singleton=True, thread_local=thread_local))
            handed_out[0].acquire()

    sys.setprofile(take_from_cache)
    try:
        FileLock(lock_path, is_singleton=True, thread_local=thread_local)
    finally:
        sys.setprofile(None)
    return handed_out.pop()


@NEEDS_PROMPT_FINALIZATION
@pytest.mark.parametrize("thread_local", [pytest.param(True, id="thread_local"), pytest.param(False, id="shared")])
def test_singleton_handed_out_while_finalizing_releases_once_dropped(tmp_path: Path, thread_local: bool) -> None:
    lock_path: Final[str] = str(tmp_path / "a")
    handed_out = _singleton_handed_out_while_finalizing(lock_path, thread_local=thread_local)
    del handed_out
    with FileLock(lock_path, blocking=False) as other:
        assert other.is_locked


@pytest.mark.parametrize("lock_type", [FileLock, SoftFileLock])
def test_concurrent_force_releases_free_an_exited_threads_hold_once(
    lock_type: type[BaseFileLock], tmp_path: Path, mocker: MockerFixture, caplog: pytest.LogCaptureFixture
) -> None:
    lock: Final[BaseFileLock] = lock_type(str(tmp_path / "a"))
    _acquire_in_exited_thread(lock)
    first_inside: Final[threading.Event] = threading.Event()

    def stall_the_first_release(record: logging.LogRecord) -> bool:
        # A slow log handler holds the first releaser inside the release while the second one arrives.
        if record.getMessage().startswith("Attempting to release") and not first_inside.is_set():
            first_inside.set()
            time.sleep(0.2)  # simplint: ignore[sleep-in-test]  # needs a release-arrival signal
        return True

    def record_close(fd: int) -> None:
        # Finalizers of other tests' leftovers close unrelated descriptors here; the inode picks out this lock's.
        inode: int | None = None
        with suppress(OSError):
            inode = os.fstat(fd).st_ino
        closes.append((fd, inode))
        real_close(fd)

    lock_inode: Final[int] = (tmp_path / "a").stat().st_ino
    closes: Final[list[tuple[int, int | None]]] = []
    real_close = os.close
    caplog.set_level(logging.DEBUG, logger="filelock")
    logging.getLogger("filelock").addFilter(stall_the_first_release)
    mocker.patch("os.close", autospec=True, side_effect=record_close)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(lock.release, force=True)
            first_inside.wait(THREAD_WAIT)
            second = pool.submit(lock.release, force=True)
    finally:
        logging.getLogger("filelock").removeFilter(stall_the_first_release)
    [lock_fd] = {fd for fd, inode in closes if inode == lock_inode}
    assert (first.exception(), second.exception(), [fd for fd, _ in closes if fd == lock_fd]) == (None, None, [lock_fd])


@pytest.mark.parametrize("lock_type", [FileLock, SoftFileLock])
def test_force_release_keeps_hold_of_live_thread(lock_type: type[BaseFileLock], tmp_path: Path) -> None:
    lock_path: Final[str] = str(tmp_path / "a")
    lock: Final[BaseFileLock] = lock_type(lock_path)
    with _held_by_live_thread([lock]):
        lock.release(force=True)
        with pytest.raises(Timeout):
            lock_type(lock_path, blocking=False).acquire()
