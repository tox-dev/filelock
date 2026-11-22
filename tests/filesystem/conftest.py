from __future__ import annotations

import os
import time
from typing import TYPE_CHECKING, Final

import pytest

from tests.conftest import DEAD_HOLDER

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def stale_lock(tmp_path: Path) -> Path:
    path: Final[Path] = tmp_path / "test.lock"
    path.write_text(DEAD_HOLDER, encoding="utf-8")
    return path


def rewrite_in_place(lock: Path) -> None:
    lock.write_text("live", encoding="utf-8")
    os.utime(lock, (time.time() + 10, time.time() + 10))


def replace_with_same_mtime(lock: Path) -> None:
    # NFS and FAT can give distinct inodes the same timestamp.
    stale: Final[os.stat_result] = lock.stat()
    (replacement := lock.with_name("recreated")).write_text("live", encoding="utf-8")
    os.utime(replacement, ns=(stale.st_atime_ns, stale.st_mtime_ns))
    replacement.replace(lock)
