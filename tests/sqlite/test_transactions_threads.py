from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from functools import partial
from typing import TYPE_CHECKING, Final

import pytest

from tests.read_write_helpers import writer_gets_in

pytest.importorskip("sqlite3")
from filelock import ReadWriteLock, Timeout

if TYPE_CHECKING:
    from collections.abc import Callable, Generator

    from filelock import AcquireReturnProxy

pytestmark: Final[pytest.MarkDecorator] = pytest.mark.usefixtures("clear_sqlite_singletons")


def test_write_lock_release_from_different_thread_prohibited(lock_file: str) -> None:
    with (
        closing(ReadWriteLock(lock_file, is_singleton=False)) as lock,
        closing(ReadWriteLock(lock_file, is_singleton=False)) as contender,
        ThreadPoolExecutor(max_workers=1) as other_thread,
    ):
        lock.acquire_write()
        with pytest.raises(RuntimeError, match="while it is held by thread"):
            other_thread.submit(lock.release).result()
        with pytest.raises(Timeout):
            contender.acquire_write(blocking=False)


def test_read_lock_release_from_a_thread_without_a_read_hold_prohibited(
    sync_locks: tuple[ReadWriteLock, ReadWriteLock], other_thread: ThreadPoolExecutor
) -> None:
    lock, contender = sync_locks
    lock.acquire_read()
    with pytest.raises(RuntimeError, match="which does not hold it"):
        other_thread.submit(lock.release).result()
    with pytest.raises(Timeout):
        contender.acquire_write(blocking=False)


def test_read_lock_release_past_a_threads_own_depth_prohibited(
    sync_locks: tuple[ReadWriteLock, ReadWriteLock], other_thread: ThreadPoolExecutor
) -> None:
    lock, contender = sync_locks
    lock.acquire_read()
    lock.acquire_read()
    other_thread.submit(lock.acquire_read).result()
    other_thread.submit(lock.release).result()
    with pytest.raises(RuntimeError, match="which does not hold it"):
        other_thread.submit(lock.release).result()
    with pytest.raises(Timeout):
        contender.acquire_write(blocking=False)


def test_read_lock_shared_by_threads_ends_with_the_last_reader(
    sync_locks: tuple[ReadWriteLock, ReadWriteLock], other_thread: ThreadPoolExecutor
) -> None:
    lock, _ = sync_locks
    lock.acquire_read()
    other_thread.submit(lock.acquire_read).result()
    lock.release()
    assert not writer_gets_in(lock.lock_file)
    other_thread.submit(lock.release).result()
    assert writer_gets_in(lock.lock_file)


@pytest.mark.parametrize(
    ("held", "requested"),
    [
        pytest.param(ReadWriteLock.acquire_read, ReadWriteLock.acquire_write, id="read-held"),
        pytest.param(ReadWriteLock.acquire_write, ReadWriteLock.acquire_read, id="write-held"),
    ],
)
def test_opposite_mode_from_another_thread_names_the_per_thread_instance_fix(
    sync_locks: tuple[ReadWriteLock, ReadWriteLock],
    other_thread: ThreadPoolExecutor,
    held: Callable[[ReadWriteLock], AcquireReturnProxy],
    requested: Callable[[ReadWriteLock], AcquireReturnProxy],
) -> None:
    held(sync_locks[0])
    with pytest.raises(RuntimeError, match="give each thread its own instance"):
        other_thread.submit(requested, sync_locks[0]).result()


def test_thread_with_its_own_instance_contends_instead_of_raising(
    sync_locks: tuple[ReadWriteLock, ReadWriteLock], other_thread: ThreadPoolExecutor
) -> None:
    lock, own = sync_locks
    lock.acquire_read()
    with pytest.raises(Timeout):
        other_thread.submit(partial(own.acquire_write, blocking=False)).result()


@pytest.fixture
def other_thread() -> Generator[ThreadPoolExecutor]:
    with ThreadPoolExecutor(max_workers=1) as executor:
        yield executor


@pytest.mark.parametrize(
    "drop",
    [
        pytest.param(ReadWriteLock.close, id="close"),
        pytest.param(partial(ReadWriteLock.release, force=True), id="force-release"),
    ],
)
def test_write_lock_drop_from_different_thread_needs_an_explicit_override(
    lock_file: str, drop: Callable[[ReadWriteLock], None]
) -> None:
    with (
        closing(ReadWriteLock(lock_file, is_singleton=False)) as lock,
        ThreadPoolExecutor(max_workers=1) as other_thread,
    ):
        lock.acquire_write()
        other_thread.submit(drop, lock).result()
        assert writer_gets_in(lock_file)
