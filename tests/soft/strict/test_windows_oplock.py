from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING, Final

import pytest

from filelock import (
    StrictSoftFileLock,
    Timeout,
)
from filelock._soft._strict._claims import _PRIVATE_RECORD_MARKER
from tests.process_helpers import call_within

if TYPE_CHECKING:
    from collections.abc import Callable

    from pytest_mock import MockerFixture

    from tests.conftest import OplockHolder


pytestmark: Final[pytest.MarkDecorator] = pytest.mark.requires_hard_links


@pytest.mark.skipif(os.name != "nt", reason="oplocks exist only on Windows")
def test_strict_soft_sentinel_oplock_keeps_deadline(
    tmp_path: Path, hold_oplock: Callable[[Path], Callable[[], None]]
) -> None:  # pragma: win32 cover
    lock_path = tmp_path / "resource.lock"
    with StrictSoftFileLock(lock_path):
        pass
    close_holder = hold_oplock(lock_path)
    try:
        finished, error = call_within(lambda: StrictSoftFileLock(lock_path).acquire(timeout=0.1), 2)
    finally:
        close_holder()
    assert (finished, type(error)) == (True, Timeout)


@pytest.mark.skipif(sys.platform != "win32", reason="oplocks exist only on Windows")
@pytest.mark.parametrize("phase", ["intent", "held"])
@pytest.mark.parametrize("timeout", [0.0, 0.05, 0.2, 0.75])
def test_strict_soft_claim_publication_oplock_keeps_deadline(  # pragma: win32 cover
    tmp_path: Path, hold_oplock: OplockHolder, mocker: MockerFixture, phase: str, timeout: float
) -> None:
    assert sys.platform == "win32"
    from filelock._soft._strict import _windows as strict_windows

    rename = strict_windows.rename_open_file
    holders: list[Callable[[], None]] = []
    rename_started = 0.0

    def hold_claim_rename(fd: int, public_path: str) -> None:
        nonlocal rename_started
        if not holders and Path(public_path).name.startswith(phase):
            private = next(Path(public_path).parent.glob(f".{Path(public_path).name}{_PRIVATE_RECORD_MARKER}*.tmp"))
            holders.append(hold_oplock(private, read_handle=True))
            rename_started = time.perf_counter()
        rename(fd, public_path)

    mocker.patch("filelock._soft._strict._windows.rename_open_file", side_effect=hold_claim_rename, autospec=True)
    lock = StrictSoftFileLock(tmp_path / "resource.lock", timeout=timeout)
    try:
        finished, error = call_within(lock.acquire, 5)
        assert (finished, type(error)) == (True, Timeout)
        # Exclude worker startup and reader setup from the rename deadline.
        assert time.perf_counter() - rename_started < timeout + 0.4

        def acquire_again() -> None:
            with lock.acquire(timeout=0):
                assert lock.is_locked

        finished, error = call_within(acquire_again, 2)
    finally:
        for close_holder in holders:
            close_holder()
    assert (finished, error, lock.claims) == (True, None, ())


@pytest.mark.skipif(os.name != "nt", reason="oplocks exist only on Windows")
def test_strict_soft_private_record_oplock_keeps_deadline(
    tmp_path: Path, hold_oplock: OplockHolder
) -> None:  # pragma: win32 cover
    lock_path = tmp_path / "resource.lock"
    with StrictSoftFileLock(lock_path):
        pass
    private = Path(f"{lock_path}.filelock") / "claims" / f".held-v1-{'0' * 32}.claim.private-v1-{'0' * 32}.tmp"
    private.write_bytes(b"")
    aged = time.time() - 3600
    os.utime(private, (aged, aged))
    entered: list[bool] = []

    def acquire() -> None:
        with StrictSoftFileLock(lock_path, timeout=0):
            entered.append(True)

    close_holder = hold_oplock(private, read_handle=True)
    try:
        finished, error = call_within(acquire, 2)
    finally:
        close_holder()
    assert (finished, error, entered) == (True, None, [True])
