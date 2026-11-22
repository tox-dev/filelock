from __future__ import annotations

import sys
import threading
from contextlib import nullcontext
from errno import EACCES
from pathlib import Path
from typing import TYPE_CHECKING, Final

import pytest

from filelock import StrictSoftFileLock, Timeout
from filelock._soft._strict._publication import unlink_in_directory

if TYPE_CHECKING:
    from collections.abc import Callable
    from contextlib import AbstractContextManager

    from pytest_mock import MockerFixture

    from filelock._core import FileLockContext
from .capability_marks import WINDOWS_ONLY
from .claim_records import write_foreign_claim

pytestmark: Final[pytest.MarkDecorator] = pytest.mark.requires_hard_links


@pytest.fixture
def lost_windows_doorway(initialized_lock_path: Path, mocker: MockerFixture) -> None:  # pragma: win32 cover
    assert sys.platform == "win32"
    from filelock._soft._strict import _windows as strict_windows

    # These tests need published claims before they can exercise withdrawal.
    mocker.patch.object(strict_windows, "cancelling_oplock_wait", autospec=True, return_value=nullcontext())
    rename = strict_windows.rename_open_file
    token = mocker.patch("secrets.token_hex", autospec=True, side_effect=lambda size: "f" * (size * 2))
    winner = Path(f"{initialized_lock_path}.filelock") / "claims" / f"intent-v1-{'0' * 32}.claim"

    def publish_competing_intent(fd: int, path: str) -> None:
        rename(fd, path)
        if Path(path).name.startswith("held-"):
            mocker.stop(publication)
            mocker.stop(token)
            write_foreign_claim(winner.parent, "0" * 32, "intent")

    publication = mocker.patch.object(
        strict_windows, "rename_open_file", autospec=True, side_effect=publish_competing_intent
    )
    unlink = unlink_in_directory

    def retire_winner(
        directory: Path,
        name: str,
        *,
        claim_fd: int | None = None,
        cancel_wait: AbstractContextManager[None] | None = None,
    ) -> BaseException | None:
        mocker.stop(withdrawal)
        winner.unlink()
        return unlink(directory, name, claim_fd=claim_fd, cancel_wait=cancel_wait)

    withdrawal = mocker.patch(
        "filelock._soft._strict._publication.unlink_in_directory", side_effect=retire_winner, autospec=True
    )


@WINDOWS_ONLY  # pragma: win32 cover
@pytest.mark.usefixtures("lost_windows_doorway")
@pytest.mark.parametrize("timeout", [pytest.param(0, id="nonblocking"), pytest.param(0.05, id="timed")])
def test_strict_soft_windows_withdraws_after_timeout(
    initialized_lock_path: Path, mocker: MockerFixture, timeout: float
) -> None:
    assert sys.platform == "win32"
    import ctypes

    started = threading.Event()
    resume = threading.Event()
    from filelock._soft._strict._windows import mark_claim_for_deletion

    delete = mark_claim_for_deletion

    def delay_delete(
        fd: int,
        path: str,
        private_name: Callable[[], str] | None = None,
        *,
        cancel_wait: AbstractContextManager[None] | None = None,
    ) -> OSError | None:
        if cancel_wait is not None:
            raise ctypes.WinError(995)
        started.set()
        assert resume.wait(5)
        return delete(fd, path, private_name)

    mocker.patch("filelock._soft._strict._publication.mark_claim_for_deletion", side_effect=delay_delete, autospec=True)
    owner = StrictSoftFileLock(initialized_lock_path)
    try:
        with pytest.raises(Timeout):
            owner.acquire(timeout=timeout, poll_interval=0.005)
        assert started.wait(5)
        owner.release_with_timeout(timeout)
        assert (owner.is_locked, sorted(claim.state for claim in owner.claims)) == (False, ["held", "intent"])
        with pytest.raises(Timeout):
            owner.acquire(timeout=0.05, poll_interval=0.005)
    finally:
        resume.set()
    with StrictSoftFileLock(initialized_lock_path, is_singleton=False).acquire(timeout=5) as peer:
        assert peer.is_locked
    with owner.acquire(timeout=5):
        assert owner.is_locked


@WINDOWS_ONLY  # pragma: win32 cover
@pytest.mark.usefixtures("lost_windows_doorway")
def test_strict_soft_windows_unlimited_withdrawal(initialized_lock_path: Path, mocker: MockerFixture) -> None:
    mocker.patch(
        "filelock._soft._strict._publication.cancelling_oplock_wait",
        autospec=True,
        side_effect=AssertionError("unbounded withdrawal"),
    )
    with StrictSoftFileLock(initialized_lock_path).acquire(timeout=-1) as held:
        assert held.is_locked
    assert held.claims == ()


@WINDOWS_ONLY  # pragma: win32 cover
@pytest.mark.usefixtures("lost_windows_doorway")
def test_strict_soft_windows_reports_deferred_delete_error(initialized_lock_path: Path, mocker: MockerFixture) -> None:
    assert sys.platform == "win32"
    import ctypes

    from filelock._soft._strict._windows import mark_claim_for_deletion

    delete = mark_claim_for_deletion

    def deny_held(
        fd: int,
        path: str,
        private_name: Callable[[], str] | None = None,
        *,
        cancel_wait: AbstractContextManager[None] | None = None,
    ) -> OSError | None:
        if cancel_wait is not None:
            raise ctypes.WinError(995)
        if Path(path).name.startswith("held-"):
            raise PermissionError(EACCES, "held deletion denied")
        return delete(fd, path, private_name)

    refusal = mocker.patch(
        "filelock._soft._strict._publication.mark_claim_for_deletion", side_effect=deny_held, autospec=True
    )
    owner = StrictSoftFileLock(initialized_lock_path)
    with pytest.raises(Timeout):
        owner.acquire(blocking=False)
    with pytest.raises(PermissionError, match="held deletion denied"):
        owner.acquire(timeout=5)
    assert [claim.state for claim in owner.claims] == ["held"]
    mocker.stop(refusal)
    with owner.acquire(timeout=5):
        assert owner.is_locked
    assert owner.claims == ()


@WINDOWS_ONLY  # pragma: win32 cover
@pytest.mark.usefixtures("lost_windows_doorway")
def test_strict_soft_windows_recovers_when_withdrawal_thread_cannot_start(
    initialized_lock_path: Path, mocker: MockerFixture
) -> None:
    assert sys.platform == "win32"
    import ctypes

    start = threading.Thread.start
    refused: list[threading.Thread] = []

    def refuse_worker(thread: threading.Thread) -> None:
        if thread.name == "filelock-claim-withdrawal":
            refused.append(thread)
            msg = "could not start withdrawal"
            raise RuntimeError(msg)
        start(thread)

    mocker.patch.object(threading.Thread, "start", side_effect=refuse_worker, autospec=True)
    refusal = mocker.patch(
        "filelock._soft._strict._publication.mark_claim_for_deletion", side_effect=ctypes.WinError(995), autospec=True
    )
    owner = StrictSoftFileLock(initialized_lock_path)
    with pytest.raises(RuntimeError, match="could not start withdrawal"):
        owner.acquire(blocking=False)
    mocker.stop(refusal)
    (worker,) = refused
    worker.run()
    assert sorted(claim.state for claim in owner.claims) == ["held", "intent"]
    with owner.acquire(timeout=5):
        assert owner.is_locked
    assert owner.claims == ()


@WINDOWS_ONLY  # pragma: win32 cover
@pytest.mark.usefixtures("lost_windows_doorway")
def test_strict_soft_windows_reports_withdrawal_worker_failure(
    initialized_lock_path: Path, mocker: MockerFixture
) -> None:
    assert sys.platform == "win32"
    import ctypes

    owner = StrictSoftFileLock(initialized_lock_path)
    unlink = owner._unlink_owner_claims

    def fail_worker(state: FileLockContext, *, deadline: float | None = None) -> tuple[list[str], list[BaseException]]:
        if deadline is None:
            msg = "withdrawal worker interrupted"
            raise RuntimeError(msg)
        return unlink(state, deadline=deadline)

    interrupted = mocker.patch.object(owner, "_unlink_owner_claims", autospec=True, side_effect=fail_worker)
    refusal = mocker.patch(
        "filelock._soft._strict._publication.mark_claim_for_deletion", side_effect=ctypes.WinError(995), autospec=True
    )
    with pytest.raises(Timeout):
        owner.acquire(blocking=False)
    with pytest.raises(RuntimeError, match="withdrawal worker interrupted"):
        owner.acquire(timeout=5)
    assert sorted(claim.state for claim in owner.claims) == ["held", "intent"]
    mocker.stop(interrupted)
    mocker.stop(refusal)
    with owner.acquire(timeout=5):
        assert owner.is_locked
    assert owner.claims == ()
