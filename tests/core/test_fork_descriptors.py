from __future__ import annotations

import os
import threading
from errno import EBADF
from queue import Queue
from typing import TYPE_CHECKING, NoReturn

from filelock import (
    FileLock,
)
from tests.capability_marks import NEEDS_FORK, NEEDS_REGISTER_AT_FORK
from tests.fork_helpers import exit_child, fork_process

if TYPE_CHECKING:
    from pathlib import Path

    from pytest_mock import MockerFixture

from tests.core.fork_probes import FORK_WARNING


@NEEDS_FORK  # pragma: needs fork
@FORK_WARNING
def test_child_closes_descriptor_held_by_vanished_thread(tmp_path: Path) -> None:
    path = str(tmp_path / "parent.lock")
    descriptor: Queue[int] = Queue()
    acquired, release = threading.Event(), threading.Event()

    def hold_lock() -> None:
        with FileLock(path, thread_local=True, is_singleton=False, on_acquired=descriptor.put):
            acquired.set()
            release.wait()

    worker = threading.Thread(target=hold_lock)
    worker.start()
    assert acquired.wait(timeout=5)
    fd = descriptor.get(timeout=5)

    def descriptor_child() -> NoReturn:
        _assert_descriptor_closed(fd)

    _, status = os.waitpid(fork_process(descriptor_child), 0)
    release.set()
    worker.join(timeout=5)

    assert (os.waitstatus_to_exitcode(status), worker.is_alive()) == (0, False)


def _assert_descriptor_closed(fd: int) -> NoReturn:
    try:
        os.fstat(fd)
    except OSError as exception:
        exit_child(0 if exception.errno == EBADF else 1)
    exit_child(1)


@NEEDS_FORK  # pragma: needs fork
@FORK_WARNING
def test_fork_waits_for_descriptor_registration(tmp_path: Path, mocker: MockerFixture) -> None:
    lock = FileLock(str(tmp_path / "parent.lock"), thread_local=False, is_singleton=False)
    entered, proceed, acquired = threading.Event(), threading.Event(), threading.Event()
    descriptors: list[int] = []
    real_fstat = os.fstat

    def delayed_fstat(fd: int) -> os.stat_result:
        if threading.current_thread() is worker and not entered.is_set():
            descriptors.append(fd)
            entered.set()
            proceed.wait()
        return real_fstat(fd)

    mocker.patch("os.fstat", side_effect=delayed_fstat, autospec=True)

    def acquire_lock() -> None:
        lock.acquire()
        acquired.set()

    worker = threading.Thread(target=acquire_lock)
    worker.start()
    assert entered.wait(timeout=5)
    timer = threading.Timer(0.05, proceed.set)
    timer.start()

    def descriptor_child() -> NoReturn:
        _assert_descriptor_closed(descriptors[0])

    _, status = os.waitpid(fork_process(descriptor_child), 0)
    timer.join(timeout=5)
    worker.join(timeout=5)

    assert (os.waitstatus_to_exitcode(status), acquired.is_set(), worker.is_alive()) == (0, True, False)
    lock.release()


@NEEDS_REGISTER_AT_FORK
def test_unrelated_acquisitions_reach_filesystem_boundary_concurrently(  # pragma: needs fork
    tmp_path: Path, mocker: MockerFixture
) -> None:
    boundary = threading.Barrier(3, timeout=5)
    real_fstat = os.fstat

    def wait_at_boundary(fd: int) -> os.stat_result:
        # Only the two named workers stat while the patch is installed, so the guard never takes its false arc.
        if threading.current_thread().name.startswith("concurrent-acquire-"):  # pragma: no branch
            boundary.wait()
        return real_fstat(fd)

    mocker.patch("os.fstat", side_effect=wait_at_boundary, autospec=True)

    def acquire_lock(path: str) -> None:
        with FileLock(path, mode=0o600, is_singleton=False):
            pass

    workers = [
        threading.Thread(
            target=acquire_lock,
            args=(str(tmp_path / f"{index}.lock"),),
            name=f"concurrent-acquire-{index}",
        )
        for index in range(2)
    ]
    for worker in workers:
        worker.start()
    try:
        boundary.wait()
    finally:
        for worker in workers:
            worker.join(timeout=5)

    assert [worker.is_alive() for worker in workers] == [False, False]
