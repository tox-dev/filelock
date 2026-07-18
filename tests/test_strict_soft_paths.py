from __future__ import annotations

import asyncio
import os
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from functools import partial
from typing import TYPE_CHECKING, Final

import pytest

from filelock import AsyncStrictSoftFileLock, StrictSoftFileLock, Timeout

if TYPE_CHECKING:
    from pathlib import Path

_STRICT_SENTINEL: Final[bytes] = b"1\nfilelock-strict-v1\x00\n0\n"


pytestmark = pytest.mark.requires_hard_links


def test_strict_soft_relative_release_uses_acquisition_directory(
    working_directories: tuple[Path, Path],
) -> None:
    first, second = working_directories
    lock = StrictSoftFileLock("resource.lock")
    lock.acquire()
    assert tuple(claim.state for claim in StrictSoftFileLock(first / "resource.lock").claims) == ("held", "intent")

    os.chdir(second)
    lock.release()

    assert (
        StrictSoftFileLock(first / "resource.lock").claims,
        StrictSoftFileLock(second / "resource.lock").claims,
    ) == ((), ())


@pytest.mark.asyncio
async def test_async_strict_soft_relative_release_uses_acquisition_directory(
    working_directories: tuple[Path, Path],
) -> None:
    first, second = working_directories
    lock = AsyncStrictSoftFileLock("resource.lock", run_in_executor=False)
    await lock.acquire()
    assert tuple(claim.state for claim in StrictSoftFileLock(first / "resource.lock").claims) == ("held", "intent")

    os.chdir(second)
    await lock.release()

    assert (
        StrictSoftFileLock(first / "resource.lock").claims,
        StrictSoftFileLock(second / "resource.lock").claims,
    ) == ((), ())


def test_strict_soft_waiter_keeps_acquisition_directory(
    working_directories: tuple[Path, Path],
) -> None:
    first, second = working_directories
    holder = StrictSoftFileLock(first / "resource.lock")
    holder.acquire()
    polling = threading.Event()
    acquired = threading.Event()
    release = threading.Event()
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(_hold_relative_lock, polling, acquired, release)
            assert polling.wait(timeout=2)
            os.chdir(second)
            holder.release()
            assert acquired.wait(timeout=2)
            assert (
                tuple(claim.state for claim in StrictSoftFileLock(first / "resource.lock").claims),
                StrictSoftFileLock(second / "resource.lock").claims,
            ) == (("held", "intent"), ())
            release.set()
            future.result(timeout=2)
    finally:
        release.set()
        holder.release(force=True)


@pytest.mark.asyncio
async def test_async_strict_soft_waiter_keeps_acquisition_directory(
    working_directories: tuple[Path, Path],
) -> None:
    first, second = working_directories
    holder = AsyncStrictSoftFileLock(first / "resource.lock", run_in_executor=False)
    await holder.acquire()
    polling = asyncio.Event()
    contender = AsyncStrictSoftFileLock("resource.lock", timeout=2, poll_interval=0.001, run_in_executor=False)
    task = asyncio.create_task(contender.acquire(cancel_check=partial(_signal_async_poll, polling)))
    try:
        await asyncio.wait_for(polling.wait(), timeout=2)
        os.chdir(second)
        await holder.release()
        await asyncio.wait_for(task, timeout=2)
        assert (
            tuple(claim.state for claim in StrictSoftFileLock(first / "resource.lock").claims),
            StrictSoftFileLock(second / "resource.lock").claims,
        ) == (("held", "intent"), ())
    finally:
        if not task.done():  # pragma: no cover  # the contender wins before teardown, so cancellation cleanup is rare
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        await holder.release(force=True)
        await contender.release(force=True)


@pytest.mark.skipif(sys.platform == "win32", reason="creating symlinks requires elevated Windows privileges")
def test_strict_soft_release_uses_original_symlink_parent(tmp_path: Path) -> None:  # pragma: win32 no cover
    original = tmp_path / "original"
    replacement = tmp_path / "replacement"
    original.mkdir()
    replacement.mkdir()
    link = tmp_path / "link"
    link.symlink_to(original, target_is_directory=True)
    original_holder = StrictSoftFileLock(link / "resource.lock")
    replacement_holder = StrictSoftFileLock(replacement / "resource.lock")
    original_holder.acquire()
    replacement_holder.acquire()
    try:  # pragma: win32 no cover
        link.unlink()
        link.symlink_to(replacement, target_is_directory=True)
        original_holder.release()

        assert (
            StrictSoftFileLock(original / "resource.lock").claims,
            tuple(claim.state for claim in replacement_holder.claims),
        ) == ((), ("held", "intent"))
    finally:
        original_holder.release(force=True)
        replacement_holder.release(force=True)


@pytest.mark.skipif(sys.platform == "win32", reason="creating symlinks requires elevated Windows privileges")
def test_strict_soft_final_symlink_fails_closed_without_touching_target(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.write_bytes(_STRICT_SENTINEL)
    lock_path = tmp_path / "resource.lock"
    lock_path.symlink_to(target)
    lock = StrictSoftFileLock(lock_path, timeout=0)

    with pytest.raises(Timeout):  # pragma: win32 no cover
        lock.acquire()

    assert (target.read_bytes(), lock_path.is_symlink(), lock.claims) == (_STRICT_SENTINEL, True, ())


def _hold_relative_lock(polling: threading.Event, acquired: threading.Event, release: threading.Event) -> None:
    with StrictSoftFileLock("resource.lock", timeout=2, poll_interval=0.001).acquire(
        cancel_check=partial(_signal_thread_poll, polling)
    ):
        acquired.set()
        assert release.wait(timeout=2)


def _signal_thread_poll(polling: threading.Event) -> bool:
    polling.set()
    return False


def _signal_async_poll(polling: asyncio.Event) -> bool:
    polling.set()
    return False


@pytest.fixture
def working_directories(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    monkeypatch.chdir(first)
    return first, second
