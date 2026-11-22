from __future__ import annotations

import gc
import os
import sys
import threading
from errno import EACCES, EIO
from pathlib import Path
from typing import TYPE_CHECKING, Final, NoReturn

import pytest
from capabilities import CAPABILITIES

from filelock import StrictSoftFileLock, Timeout
from filelock._soft._strict._publication import unlink_in_directory
from tests.capability_marks import (
    NEEDS_COLLECTED_FINALIZATION,
)
from tests.descriptor_helpers import descriptors_on

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from contextlib import AbstractContextManager

    from pytest_mock import MockerFixture

    from .filesystem_faults import PathValue
from .claim_records import initialize_protocol, write_foreign_claim

pytestmark: Final[pytest.MarkDecorator] = pytest.mark.requires_hard_links


_NEEDS_FD_DIRECTORY: Final[pytest.MarkDecorator] = pytest.mark.skipif(
    not CAPABILITIES["fd-directory"], reason="counting open descriptors needs a /dev/fd or /proc/self/fd view"
)


def test_strict_soft_release_claim_unlink_failures_group(tmp_path: Path, mocker: MockerFixture) -> None:
    lock = StrictSoftFileLock(tmp_path / "resource.lock")
    lock.acquire()
    unlink_mock = mocker.patch(
        "filelock._soft._strict._publication.unlink_in_directory",
        side_effect=PermissionError(EACCES, "claim unlink denied"),
        autospec=True,
    )

    with pytest.raises(BaseExceptionGroup) as raised:
        lock.release()
    assert ([str(error) for error in raised.value.exceptions], lock.is_locked, len(lock.claims)) == (
        ["[Errno 13] claim unlink denied", "[Errno 13] claim unlink denied"],
        True,
        2,
    )

    mocker.stop(unlink_mock)
    lock.release()
    assert (lock.is_locked, lock.claims) == (False, ())


def test_strict_soft_doorway_claim_unlink_failure_stays_pending(tmp_path: Path, mocker: MockerFixture) -> None:
    lock_path = tmp_path / "resource.lock"
    initialize_protocol(lock_path)
    owner_token = "f" * 32
    competitor_name = f"held-v1-{'0' * 32}.claim"
    intent_name = f"intent-v1-{owner_token}.claim"
    real_scandir = os.scandir
    injected = False

    def add_competitor_once_intent_is_published(path: int | str) -> Iterator[os.DirEntry[str]]:
        # Platforms scan different numbers of times before intent publication.
        nonlocal injected
        if not injected:
            with real_scandir(path) as entries:
                intent_published = any(entry.name == intent_name for entry in entries)
            if intent_published:
                write_foreign_claim(Path(f"{lock_path}.filelock") / "claims", "0" * 32)
                injected = True
        return real_scandir(path)

    real_unlink_in_directory = unlink_in_directory

    def deny_only_the_owned_intent_unlink(
        directory: Path,
        name: str,
        *,
        claim_fd: int | None = None,
        cancel_wait: AbstractContextManager[None] | None = None,
    ) -> BaseException | None:
        if name == intent_name:
            raise PermissionError(EACCES, "intent unlink denied")
        return real_unlink_in_directory(directory, name, claim_fd=claim_fd, cancel_wait=cancel_wait)

    mocker.patch("secrets.token_hex", return_value=owner_token, autospec=True)
    scandir_mock = mocker.patch("os.scandir", side_effect=add_competitor_once_intent_is_published, autospec=True)
    unlink_mock = mocker.patch(
        "filelock._soft._strict._publication.unlink_in_directory",
        side_effect=deny_only_the_owned_intent_unlink,
        autospec=True,
    )
    lock = StrictSoftFileLock(lock_path)

    with pytest.raises(PermissionError, match="intent unlink denied"):
        lock.acquire()
    assert (lock.is_locked, sorted(claim.name for claim in lock.claims)) == (False, [competitor_name, intent_name])
    other = write_foreign_claim(Path(f"{lock_path}.filelock") / "claims", "1" * 32)
    mocker.patch("filelock._soft._strict._lock.unlink_in_directory", new=deny_only_the_owned_intent_unlink)
    lock.force_break(other.name)
    assert not other.exists()
    # The competitor still holds the lock while the intent awaits cleanup.
    with pytest.raises(PermissionError, match="intent unlink denied"):
        lock.acquire(timeout=0)
    mocker.stop(scandir_mock)
    mocker.stop(unlink_mock)
    with pytest.raises(Timeout):
        lock.acquire(timeout=0)
    assert ([claim.name for claim in lock.claims], lock.is_locked) == ([competitor_name], False)
    lock.force_break(competitor_name)


def _leave_pending_doorway_here(  # pragma: needs collected-finalization
    lock: StrictSoftFileLock, mocker: MockerFixture
) -> None:
    _fail_the_doorway(mocker)
    with pytest.raises(PermissionError, match="intent unlink denied"):
        lock.acquire()
    mocker.stopall()
    assert [claim.state for claim in lock.claims] == ["intent"]


def _leave_pending_doorway_in_exited_thread(lock: StrictSoftFileLock, mocker: MockerFixture) -> None:
    (linger := threading.Event()).set()
    _leave_pending_doorway(lock, mocker, linger=linger).join()


@NEEDS_COLLECTED_FINALIZATION
@pytest.mark.parametrize(
    ("leave", "thread_local"),
    [
        pytest.param(_leave_pending_doorway_here, True, id="this-thread"),
        pytest.param(_leave_pending_doorway_in_exited_thread, True, id="exited-thread"),
        pytest.param(_leave_pending_doorway_here, False, id="shared-context"),
    ],
)
def test_strict_soft_dropped_instance_removes_pending_claims(  # pragma: needs collected-finalization
    initialized_lock_path: Path,
    mocker: MockerFixture,
    leave: Callable[[StrictSoftFileLock, MockerFixture], None],
    thread_local: bool,
) -> None:
    lock = StrictSoftFileLock(initialized_lock_path, thread_local=thread_local)
    leave(lock, mocker)

    del lock
    gc.collect()

    assert StrictSoftFileLock(initialized_lock_path).claims == ()


@_NEEDS_FD_DIRECTORY  # pragma: needs fd-directory
def test_strict_soft_failed_doorway_cleanup_keeps_no_descriptor(
    initialized_lock_path: Path, mocker: MockerFixture
) -> None:
    real_link = os.link

    def fail_held_link(
        source: PathValue,
        destination: PathValue,
        *,
        src_dir_fd: int | None = None,
        dst_dir_fd: int | None = None,
        follow_symlinks: bool = True,
    ) -> None:
        if Path(os.fsdecode(destination)).name.startswith("held-"):
            raise OSError(EIO, "held link failed")
        real_link(source, destination, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd, follow_symlinks=follow_symlinks)

    mocker.patch("os.link", autospec=True, side_effect=fail_held_link)
    _deny_claim_unlinks(mocker)
    lock: Final[StrictSoftFileLock] = StrictSoftFileLock(initialized_lock_path)

    with pytest.raises(BaseExceptionGroup, match="strict doorway claim cleanup failed"):
        lock.acquire()
    assert (descriptors_on(initialized_lock_path.stat()), lock.is_locked) == (0, False)


def _deny_claim_unlinks(mocker: MockerFixture) -> None:  # pragma: needs fd-directory
    real_unlink = os.unlink

    def deny(path: PathValue, *, dir_fd: int | None = None) -> None:
        if os.fsdecode(path).endswith(".claim"):
            raise PermissionError(EACCES, "claim unlink denied")
        real_unlink(path, dir_fd=dir_fd)

    mocker.patch("os.unlink", autospec=True, side_effect=deny)


@_NEEDS_FD_DIRECTORY  # pragma: needs fd-directory
@NEEDS_COLLECTED_FINALIZATION
@pytest.mark.parametrize("thread_local", [pytest.param(True, id="thread-local"), pytest.param(False, id="shared")])
def test_strict_soft_dropped_hold_with_stuck_claims_closes_its_descriptor(  # pragma: needs collected-finalization
    initialized_lock_path: Path, mocker: MockerFixture, thread_local: bool
) -> None:
    lock = StrictSoftFileLock(initialized_lock_path, thread_local=thread_local)
    lock.acquire()
    _deny_claim_unlinks(mocker)

    del lock
    gc.collect()

    assert descriptors_on(initialized_lock_path.stat()) == 0


def test_strict_soft_acquire_leaves_a_live_threads_pending_doorway_alone(
    initialized_lock_path: Path, mocker: MockerFixture
) -> None:
    lock: Final[StrictSoftFileLock] = StrictSoftFileLock(initialized_lock_path, timeout=0)
    linger: Final[threading.Event] = threading.Event()
    worker: Final[threading.Thread] = _leave_pending_doorway(lock, mocker, linger=linger)
    try:
        # The worker finishes its own doorway on its next acquire; finishing it here would race that.
        with pytest.raises(Timeout):
            lock.acquire()
        assert [claim.state for claim in lock.claims] == ["intent"]
    finally:
        linger.set()
        worker.join()


def test_strict_soft_acquire_finishes_an_exited_threads_pending_doorway(
    initialized_lock_path: Path, mocker: MockerFixture
) -> None:
    lock: Final[StrictSoftFileLock] = StrictSoftFileLock(initialized_lock_path, timeout=0)
    _leave_pending_doorway_in_exited_thread(lock, mocker)

    with lock:
        assert [claim.state for claim in lock.claims] == ["held", "intent"]
    assert lock.claims == ()


def _leave_pending_doorway(
    lock: StrictSoftFileLock, mocker: MockerFixture, *, linger: threading.Event
) -> threading.Thread:
    _fail_the_doorway(mocker)
    failed: Final[threading.Event] = threading.Event()

    def acquire_then_linger() -> None:
        with pytest.raises(PermissionError, match="intent unlink denied"):
            lock.acquire()
        failed.set()
        linger.wait()

    (worker := threading.Thread(target=acquire_then_linger)).start()
    failed.wait()
    mocker.stopall()
    assert [claim.state for claim in lock.claims] == ["intent"]
    return worker


def _fail_the_doorway(mocker: MockerFixture) -> None:
    real_unlink_in_directory = unlink_in_directory

    def deny_intent_unlink(
        directory: Path,
        name: str,
        *,
        claim_fd: int | None = None,
        cancel_wait: AbstractContextManager[None] | None = None,
    ) -> BaseException | None:
        if name.startswith("intent-"):
            raise PermissionError(EACCES, "intent unlink denied")
        return real_unlink_in_directory(  # pragma: win32 no cover
            directory, name, claim_fd=claim_fd, cancel_wait=cancel_wait
        )

    def fail_held_link(_directory: Path, _source: str, _destination: str) -> NoReturn:
        # Raise a fresh error each call: a shared side_effect instance would keep its traceback, and with it the lock.
        raise OSError(EIO, "held link failed")

    if sys.platform == "win32":  # pragma: win32 cover
        from filelock._soft._strict._windows import publish_deletable_claim

        real_publish = publish_deletable_claim

        def fail_held_publish(
            private_path: Callable[[], str],
            public_path: str,
            record: bytes,
            descriptors: dict[str, int],
            deadline: float | None,
        ) -> int:
            if Path(public_path).name.startswith("held-"):
                raise OSError(EIO, "held link failed")
            return real_publish(private_path, public_path, record, descriptors, deadline)

        mocker.patch(
            "filelock._soft._strict._lock.publish_deletable_claim", autospec=True, side_effect=fail_held_publish
        )
    else:  # pragma: win32 no cover
        mocker.patch("filelock._soft._strict._lock.link_no_replace", autospec=True, side_effect=fail_held_link)
    mocker.patch(
        "filelock._soft._strict._publication.unlink_in_directory", autospec=True, side_effect=deny_intent_unlink
    )
