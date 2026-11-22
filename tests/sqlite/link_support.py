from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING, Final

import pytest

pytest.importorskip("sqlite3")
from filelock import ReadWriteLock
from filelock._identity import host_name

if TYPE_CHECKING:
    from pathlib import Path


def link_directories(shared_database: Path) -> list[Path]:
    return sorted(path for path in shared_database.parent.iterdir() if path.name.startswith(".filelock-"))


def host_digest(shared_database: Path, *, legacy: bool = False) -> str:
    if legacy:  # pragma: linux cover
        return hashlib.sha256(host_name().encode()).hexdigest()[:16]
    # Taken from a link of another database, so the sweep for this one has not run yet.
    seed: Final[Path] = shared_database.parent.with_name("seed")
    seed.mkdir(mode=0o777)
    seed.chmod(0o777)
    with ReadWriteLock(seed / "lock.db", is_singleton=False).read_lock():
        return next(seed.glob(".filelock-*")).name.rsplit("-", 2)[1]


__all__ = [
    "host_digest",
    "link_directories",
]
