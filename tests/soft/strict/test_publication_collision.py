from __future__ import annotations

import os
from errno import EEXIST, EXDEV
from pathlib import Path
from typing import TYPE_CHECKING, Final

import pytest

from filelock import SoftFileLockProtocolError, StrictSoftFileLock, Timeout

if TYPE_CHECKING:
    from pytest_mock import MockerFixture

    from .filesystem_faults import PathValue
from .capability_marks import CLAIM_LINK_ONLY
from .claim_records import initialize_protocol, write_foreign_claim

pytestmark: Final[pytest.MarkDecorator] = pytest.mark.requires_hard_links


def test_strict_soft_claims_empty_before_first_acquire(tmp_path: Path) -> None:
    assert StrictSoftFileLock(tmp_path / "resource.lock").claims == ()


@CLAIM_LINK_ONLY  # pragma: win32 no cover
def test_strict_soft_intent_token_collision_does_not_claim_lock(tmp_path: Path, mocker: MockerFixture) -> None:
    lock_path = tmp_path / "resource.lock"
    initialize_protocol(lock_path)

    def collide_intent(
        _source: PathValue,
        _destination: PathValue,
        **_options: int | bool | None,
    ) -> None:
        # Intent publication attempts the first link.
        raise FileExistsError(EEXIST, "claim exists")

    mocker.patch("os.link", side_effect=collide_intent, autospec=True)
    lock = StrictSoftFileLock(lock_path, timeout=0)

    with pytest.raises(Timeout):
        lock.acquire()
    assert (lock.claims, lock.is_locked) == ((), False)


@CLAIM_LINK_ONLY  # pragma: win32 no cover
def test_strict_soft_held_publication_failure_rolls_back_intent(tmp_path: Path, mocker: MockerFixture) -> None:
    lock_path = tmp_path / "resource.lock"
    initialize_protocol(lock_path)
    real_link = os.link

    def fail_held(
        source: PathValue,
        destination: PathValue,
        *,
        src_dir_fd: int | None = None,
        dst_dir_fd: int | None = None,
        follow_symlinks: bool = True,
    ) -> None:
        if Path(os.fsdecode(destination)).name.startswith("held-"):
            raise OSError(EXDEV, "hard links unavailable")
        real_link(
            source,
            destination,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
            follow_symlinks=follow_symlinks,
        )

    mocker.patch("os.link", side_effect=fail_held, autospec=True)
    lock = StrictSoftFileLock(lock_path)

    with pytest.raises(SoftFileLockProtocolError, match="hard-link publication"):
        lock.acquire()
    assert (lock.claims, lock.is_locked) == ((), False)


@CLAIM_LINK_ONLY  # pragma: win32 no cover
def test_strict_soft_held_collision_does_not_delete_foreign_claim(tmp_path: Path, mocker: MockerFixture) -> None:
    lock_path = tmp_path / "resource.lock"
    initialize_protocol(lock_path)
    token = "f" * 32
    real_link = os.link

    def publish_foreign_held(
        source: PathValue,
        destination: PathValue,
        *,
        src_dir_fd: int | None = None,
        dst_dir_fd: int | None = None,
        follow_symlinks: bool = True,
    ) -> None:
        if Path(os.fsdecode(destination)).name.startswith("held-"):
            write_foreign_claim(Path(f"{lock_path}.filelock") / "claims", token)
            raise FileExistsError(EEXIST, "foreign claim won")
        real_link(
            source,
            destination,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
            follow_symlinks=follow_symlinks,
        )

    mocker.patch("secrets.token_hex", return_value=token, autospec=True)
    mocker.patch("os.link", side_effect=publish_foreign_held, autospec=True)
    lock = StrictSoftFileLock(lock_path)

    with pytest.raises(FileExistsError, match="foreign claim won"):
        lock.acquire()
    assert ([(claim.state, claim.start) for claim in lock.claims], lock.is_locked) == ([("held", 4242)], False)
