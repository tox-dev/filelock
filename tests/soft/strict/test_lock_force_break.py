from __future__ import annotations

from pathlib import Path
from typing import Final

import pytest

from filelock import (
    SoftFileLockProtocolError,
    StrictSoftFileLock,
)

pytestmark: Final[pytest.MarkDecorator] = pytest.mark.requires_hard_links


@pytest.mark.parametrize(
    "claim_name",
    [
        pytest.param("", id="empty"),
        pytest.param(".private.tmp", id="private"),
        pytest.param("../claim", id="parent"),
        pytest.param("directory/claim", id="slash"),
        pytest.param(r"directory\claim", id="backslash"),
        pytest.param("claim\0name", id="null"),
    ],
)
def test_strict_soft_force_break_rejects_non_basename(tmp_path: Path, claim_name: str) -> None:
    with pytest.raises(ValueError, match="one public claim basename"):
        StrictSoftFileLock(tmp_path / "resource.lock").force_break(claim_name)


def test_strict_soft_force_break_names_guarantee_loss(tmp_path: Path) -> None:
    lock_path = tmp_path / "resource.lock"
    holder = StrictSoftFileLock(lock_path)
    holder.acquire()

    breaker = StrictSoftFileLock(lock_path)
    for claim in holder.claims:
        breaker.force_break(claim.name)
    with StrictSoftFileLock(lock_path, timeout=0) as contender:
        assert (holder.is_locked, contender.is_locked) == (True, True)
    holder.release()


def test_strict_soft_force_break_requires_exact_case(tmp_path: Path) -> None:
    holder = StrictSoftFileLock(tmp_path / "resource.lock")
    holder.acquire()
    names = [claim.name for claim in holder.claims]

    with pytest.raises(FileNotFoundError):
        StrictSoftFileLock(holder.lock_file).force_break(names[0].upper())
    assert [claim.name for claim in holder.claims] == names
    holder.release()


def test_strict_soft_force_break_accepts_newer_version_claim(tmp_path: Path) -> None:
    lock_path = tmp_path / "resource.lock"
    claims = Path(f"{lock_path}.filelock") / "claims"
    claims.mkdir(parents=True)
    claim_name = "held-v2-opaque.claim"
    (claims / claim_name).write_text("filelock-strict-v2\n", encoding="ascii", newline="")
    lock = StrictSoftFileLock(lock_path, timeout=0)

    with pytest.raises(SoftFileLockProtocolError, match="unknown claim name or protocol version"):
        lock.acquire()
    lock.force_break(claim_name)
    with lock:
        assert lock.is_locked
