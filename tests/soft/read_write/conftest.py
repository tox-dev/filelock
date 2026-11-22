from __future__ import annotations

import errno
from contextlib import nullcontext
from pathlib import Path
from typing import TYPE_CHECKING, Final, Literal

import pytest

from filelock import SoftReadWriteLock
from filelock._soft._read_write import _storage as storage_mod
from filelock._soft._read_write._generations import GenerationLog, Snapshot
from filelock._soft._read_write._participant import Participant
from filelock._soft._read_write._storage import OsFiles
from filelock._soft._read_write._sweep import Ledger

from .lock_helpers import make_lock

if TYPE_CHECKING:
    from collections.abc import Callable, Generator

    from pytest_mock import MockerFixture, MockType


@pytest.fixture
def flushes(mocker: MockerFixture) -> MockType:
    # Flushing hundreds of commits can stall Windows test runners.
    return mocker.patch("filelock._soft._read_write._storage.os.fsync", autospec=True)


@pytest.fixture
def failing_release(mocker: MockerFixture) -> None:
    # A full disk can fail the write after the sync lock drops its hold.
    real_release = SoftReadWriteLock._release_level_of

    def release(lock: SoftReadWriteLock, holder: int) -> None:
        real_release(lock, holder)
        raise OSError(errno.ENOSPC, "No space left on device")

    mocker.patch.object(SoftReadWriteLock, "_release_level_of", autospec=True, side_effect=release)


@pytest.fixture
def files(lock_file: str) -> OsFiles:
    return OsFiles(lock_file)


@pytest.fixture
def root(lock_file: str) -> str:
    return f"{lock_file}.rw"


@pytest.fixture
def head(root: str) -> Path:
    return Path(root, "gen", "HEAD")


@pytest.fixture
def clear_singletons() -> Generator[None]:
    SoftReadWriteLock._instances.clear()
    yield
    for lock in filter(None, (ref() for ref in list(SoftReadWriteLock._instances.valuerefs()))):
        lock.close()
    SoftReadWriteLock._instances.clear()


@pytest.fixture
def lock_file(tmp_path: Path) -> str:
    return str(tmp_path / "test.lock")


@pytest.fixture
def lock(lock_file: str) -> Generator[SoftReadWriteLock]:
    instance = make_lock(lock_file)
    try:
        yield instance
    finally:
        instance.close()


@pytest.fixture(params=["platform", "names"])
def directory_access(request: pytest.FixtureRequest, mocker: MockerFixture) -> None:
    if request.param == "names":
        mocker.patch.object(storage_mod, "_SUPPORTS_DIR_FD", new=False)
        mocker.patch.object(storage_mod, "pin_directories", autospec=True, return_value=nullcontext())


FIRST: Final[str] = "0123456789abcdef0123456789abcdef"


SECOND: Final[str] = "fedcba9876543210fedcba9876543210"


def committed(files: OsFiles, lock_file: str, root: str, writers: list[str | None]) -> GenerationLog:
    files.prepare(root)
    log = GenerationLog(files, lock_file, root)
    for writer in writers:
        assert log.commit(Snapshot(generation=log.latest().generation + 1, writer=writer, readers=frozenset()))
    return log


def make_participant(
    files: OsFiles,
    lock_file: str,
    mode: Literal["read", "write"],
    *,
    stale_threshold: float = 100,
    clock: Callable[[], float] = lambda: 0.0,
    lease_clock: Callable[[], float] | None = None,
) -> Participant:
    root = f"{lock_file}.rw"
    return Participant(
        files,
        root,
        mode,
        stale_threshold=stale_threshold,
        clock=clock,
        lease_clock=clock if lease_clock is None else lease_clock,
        ledger=Ledger(clock),
        log=GenerationLog(files, lock_file, root),
    )
