from __future__ import annotations

import os
import threading
import time
from errno import EIO
from pathlib import Path
from typing import TYPE_CHECKING, Literal

import pytest

from filelock import StrictSoftFileLock, Timeout

if TYPE_CHECKING:
    from pytest_mock import MockerFixture


def test_strict_soft_lower_intent_delayed_until_higher_selects(tmp_path: Path, mocker: MockerFixture) -> None:
    lock_path = tmp_path / "resource.lock"
    _initialize_protocol(lock_path)
    lower_intent_waiting = threading.Event()
    higher_held_attempted = threading.Event()
    lower_held_linked = threading.Event()
    higher_held_linked = threading.Event()
    real_link = os.link

    def ordered_link(
        source: str,
        destination: str,
        *,
        src_dir_fd: int | None = None,
        dst_dir_fd: int | None = None,
        follow_symlinks: bool = True,
    ) -> None:
        name = Path(destination).name
        if threading.current_thread().name == "lower" and name.startswith("intent-"):
            lower_intent_waiting.set()
            assert higher_held_attempted.wait(timeout=2)
        if threading.current_thread().name == "higher" and name.startswith("held-"):
            higher_held_attempted.set()
            assert lower_held_linked.wait(timeout=2)
        real_link(
            source,
            destination,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
            follow_symlinks=follow_symlinks,
        )
        if threading.current_thread().name == "lower" and name.startswith("held-"):
            lower_held_linked.set()
            assert higher_held_linked.wait(timeout=2)
        elif threading.current_thread().name == "higher" and name.startswith("held-"):
            higher_held_linked.set()

    mocker.patch("filelock._strict.os.link", side_effect=ordered_link)
    mocker.patch(
        "filelock._strict.secrets.token_hex",
        side_effect=lambda _size: "0" * 32 if threading.current_thread().name == "lower" else "f" * 32,
    )
    acquisition_order: list[str] = []
    inside, state_lock = 0, threading.Lock()

    def acquire(label: str) -> None:
        nonlocal inside
        with StrictSoftFileLock(lock_path, timeout=3, poll_interval=0.001):
            with state_lock:
                inside += 1
                assert inside == 1
                acquisition_order.append(label)
            time.sleep(0.01)
            with state_lock:
                inside -= 1

    lower = threading.Thread(target=acquire, args=("lower",), name="lower")
    higher = threading.Thread(target=acquire, args=("higher",), name="higher")
    lower.start()
    assert lower_intent_waiting.wait(timeout=2)
    higher.start()
    _join_threads(lower, higher)
    assert (acquisition_order, inside) == (["lower", "higher"], 0)


def test_strict_soft_lower_intent_delayed_until_higher_enters(tmp_path: Path, mocker: MockerFixture) -> None:
    lock_path = tmp_path / "resource.lock"
    _initialize_protocol(lock_path)
    lower_intent_waiting = threading.Event()
    higher_entered = threading.Event()
    release_higher = threading.Event()
    real_link = os.link

    def delayed_link(
        source: str,
        destination: str,
        *,
        src_dir_fd: int | None = None,
        dst_dir_fd: int | None = None,
        follow_symlinks: bool = True,
    ) -> None:
        if threading.current_thread().name == "lower" and Path(destination).name.startswith("intent-"):
            lower_intent_waiting.set()
            assert higher_entered.wait(timeout=2)
        real_link(
            source,
            destination,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
            follow_symlinks=follow_symlinks,
        )

    mocker.patch("filelock._strict.os.link", side_effect=delayed_link)
    mocker.patch(
        "filelock._strict.secrets.token_hex",
        side_effect=lambda _size: "0" * 32 if threading.current_thread().name == "lower" else "f" * 32,
    )
    acquisition_order: list[str] = []

    def acquire(label: str) -> None:
        with StrictSoftFileLock(lock_path, timeout=3, poll_interval=0.001):
            acquisition_order.append(label)
            if label == "higher":
                higher_entered.set()
                assert release_higher.wait(timeout=2)

    lower = threading.Thread(target=acquire, args=("lower",), name="lower")
    higher = threading.Thread(target=acquire, args=("higher",), name="higher")
    lower.start()
    assert lower_intent_waiting.wait(timeout=2)
    higher.start()
    assert higher_entered.wait(timeout=2)
    assert acquisition_order == ["higher"]
    release_higher.set()
    _join_threads(lower, higher)
    assert acquisition_order == ["higher", "lower"]


def test_strict_soft_private_partial_record_is_not_a_claim(tmp_path: Path, mocker: MockerFixture) -> None:
    lock_path = tmp_path / "resource.lock"
    _initialize_protocol(lock_path)
    partial_written = threading.Event()
    resume_write = threading.Event()
    real_write = os.write
    paused = False

    def paused_write(fd: int, data: bytes | bytearray | memoryview) -> int:
        nonlocal paused
        if threading.current_thread().name != "partial" or paused:
            return real_write(fd, data)
        paused = True
        written = real_write(fd, data[:1])
        partial_written.set()
        assert resume_write.wait(timeout=2)
        return written

    mocker.patch("filelock._util.os.write", side_effect=paused_write)

    def acquire_partial() -> None:
        with StrictSoftFileLock(lock_path, timeout=3, poll_interval=0.001):
            pass

    partial = threading.Thread(target=acquire_partial, name="partial")
    partial.start()
    assert partial_written.wait(timeout=2)
    with StrictSoftFileLock(lock_path, timeout=0) as contender:
        assert contender.is_locked
    resume_write.set()
    _join_threads(partial)


def test_strict_soft_shared_instance_waits_for_failed_doorway(tmp_path: Path, mocker: MockerFixture) -> None:
    lock_path = tmp_path / "resource.lock"
    _initialize_protocol(lock_path)
    lock = StrictSoftFileLock(lock_path, thread_local=False, timeout=2, poll_interval=0.001)
    first_doorway = threading.Event()
    fail_first = threading.Event()
    second_entered = threading.Event()
    first_errors: list[OSError] = []
    real_link = os.link

    def pause_first_intent(
        source: str,
        destination: str,
        *,
        src_dir_fd: int | None = None,
        dst_dir_fd: int | None = None,
        follow_symlinks: bool = True,
    ) -> None:
        if threading.current_thread().name == "first" and Path(destination).name.startswith("intent-"):
            first_doorway.set()
            assert fail_first.wait(timeout=2)
            raise OSError(EIO, "doorway failed")
        real_link(
            source,
            destination,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
            follow_symlinks=follow_symlinks,
        )

    def acquire_first() -> None:
        try:
            lock.acquire()
        except OSError as error:
            first_errors.append(error)

    def acquire_second() -> None:
        with lock:
            second_entered.set()

    mocker.patch("filelock._strict.os.link", side_effect=pause_first_intent)
    first = threading.Thread(target=acquire_first, name="first")
    second = threading.Thread(target=acquire_second, name="second")
    first.start()
    assert first_doorway.wait(timeout=2)
    second.start()
    assert not second_entered.wait(timeout=0.05)
    fail_first.set()
    _join_threads(first, second)
    assert ([str(error) for error in first_errors], second_entered.is_set(), lock.is_locked) == (
        ["[Errno 5] doorway failed"],
        True,
        False,
    )


@pytest.mark.parametrize(
    "admission",
    [
        pytest.param("timeout", id="zero-timeout"),
        pytest.param("blocking", id="nonblocking"),
        pytest.param("cancel", id="cancel-check"),
    ],
)
def test_strict_soft_shared_instance_transition_respects_admission(
    tmp_path: Path,
    mocker: MockerFixture,
    admission: Literal["timeout", "blocking", "cancel"],
) -> None:
    lock_path = tmp_path / "resource.lock"
    _initialize_protocol(lock_path)
    lock = StrictSoftFileLock(lock_path, thread_local=False, timeout=2, poll_interval=0.001)
    first_doorway = threading.Event()
    continue_first = threading.Event()
    release_first = threading.Event()
    real_link = os.link

    def pause_first_intent(
        source: str,
        destination: str,
        *,
        src_dir_fd: int | None = None,
        dst_dir_fd: int | None = None,
        follow_symlinks: bool = True,
    ) -> None:
        if threading.current_thread().name == "first" and Path(destination).name.startswith("intent-"):
            first_doorway.set()
            assert continue_first.wait(timeout=2)
        real_link(
            source,
            destination,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
            follow_symlinks=follow_symlinks,
        )

    def acquire_first() -> None:
        with lock:
            assert release_first.wait(timeout=2)

    mocker.patch("filelock._strict.os.link", side_effect=pause_first_intent)
    first = threading.Thread(target=acquire_first, name="first")
    first.start()
    assert first_doorway.wait(timeout=2)
    started = time.perf_counter()
    with pytest.raises(Timeout):
        _acquire_during_transition(lock, admission)
    elapsed = time.perf_counter() - started
    continue_first.set()
    release_first.set()
    _join_threads(first)
    assert (elapsed < 0.2, lock.is_locked) == (True, False)


def _acquire_during_transition(
    lock: StrictSoftFileLock,
    admission: Literal["timeout", "blocking", "cancel"],
) -> None:
    if admission == "timeout":
        lock.acquire(timeout=0)
    elif admission == "blocking":
        lock.acquire(blocking=False)
    else:
        lock.acquire(cancel_check=lambda: True)


def _initialize_protocol(lock_path: Path) -> None:
    with StrictSoftFileLock(lock_path):
        pass


def _join_threads(*threads: threading.Thread) -> None:
    for thread in threads:
        thread.join(timeout=3)
    assert [thread.name for thread in threads if thread.is_alive()] == []
