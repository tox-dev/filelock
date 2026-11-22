from __future__ import annotations

import os
import threading
from typing import TYPE_CHECKING

import pytest

from filelock import ReleasePendingError, StrictSoftFileLock

from .claim_records import initialize_protocol, private_claim_path

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from pytest_mock import MockerFixture


@pytest.fixture  # pragma: needs hard-link
def initialized_lock_path(tmp_path: Path) -> Path:
    initialize_protocol(path := tmp_path / "resource.lock")
    return path


@pytest.fixture  # pragma: needs hard-link
def abandoned_record(initialized_lock_path: Path) -> tuple[Path, Path]:
    (private_path := private_claim_path(initialized_lock_path, "0" * 32)).write_bytes(b"abandoned")
    os.utime(private_path, (0, 0))
    return initialized_lock_path, private_path


@pytest.fixture
def lock_path(tmp_path: Path) -> Path:
    return tmp_path / "strict.lock"


@pytest.fixture  # pragma: win32 cover
def deferred_release(
    lock_path: Path, mocker: MockerFixture
) -> Iterator[tuple[StrictSoftFileLock, ReleasePendingError, threading.Event]]:
    lock = StrictSoftFileLock(lock_path)
    lock.acquire()
    proceed = threading.Event()
    release = lock._release

    def pause() -> None:
        assert proceed.wait(5)
        release()

    mocker.patch.object(lock, "_release", autospec=True, side_effect=pause)
    try:
        with pytest.raises(ReleasePendingError) as pending:
            lock.release_with_timeout(0)
        yield lock, pending.value, proceed
    finally:
        proceed.set()
        lock.release()
