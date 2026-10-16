from __future__ import annotations

import gc
import itertools
import os
import secrets
import shutil
import sys
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import TYPE_CHECKING, Final, TypeVar

import pytest
from capabilities import CAPABILITIES

import filelock._identity
from filelock import BaseFileLock, MarkerSoftFileLock, SoftFileLease, SoftFileLock, Timeout
from filelock._identity import host_name

if TYPE_CHECKING:
    from collections.abc import Callable, Generator, Iterator

    from pytest_mock import MockerFixture

# Coverage restarts in every patched subprocess and re-imports the plugin there; pytest's pythonpath reaches only
# this process, so export it.
os.environ["PYTHONPATH"] = os.pathsep.join(
    part for part in (str(Path(__file__).parent.parent / "tasks"), os.environ.get("PYTHONPATH")) if part
)

_LockT = TypeVar("_LockT", bound=BaseFileLock)


def acquire_after_reclaim(lock: BaseFileLock) -> None:
    """
    Take a path whose stale marker the lock has to break first, without a wall-clock budget.

    A try finds the marker and breaks it, or first watches an unreadable one age; the last creates its own. A deadline
    would test how fast the machine is: a first try that outlasts it times out after the marker is gone and before the
    lock is held.
    """
    for _ in range(3):
        with suppress(Timeout):
            lock.acquire(blocking=False)
        if lock.is_locked:
            return


@contextmanager
def held_after_reclaim(lock: _LockT) -> Generator[_LockT]:
    """Hold *lock* through :func:`acquire_after_reclaim` and release it on exit."""
    acquire_after_reclaim(lock)
    try:
        yield lock
    finally:
        lock.release()


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    # StrictSoftFileLock publishes claims with hard links, so tests marked requires_hard_links cannot run where
    # os.link is missing (Termux/Android CPython ships without it). The no-hard-link degradation itself is covered by
    # the os.link tests in test_filelock.py, which carry no marker and run everywhere.
    if not hasattr(os, "link"):  # pragma: no cover  # the body runs only on Termux/Android, absent from the CI matrix
        skip = pytest.mark.skip(reason="StrictSoftFileLock requires os.link (hard links), absent on Termux/Android")
        for item in items:
            if item.get_closest_marker("requires_hard_links") is not None:
                item.add_marker(skip)


@pytest.fixture(autouse=True, scope="session")
def _tracemalloc_lookup_without_support() -> Iterator[None]:
    # pytest's thread and unraisable exception hooks ask tracemalloc where the offending object was allocated. Where
    # that lookup raises (GraalPy), the hook reports "Failed to process thread exception" in place of the thread's real
    # error, so hand it the answer tracemalloc gives while it is not tracing.
    with pytest.MonkeyPatch.context() as patch:
        if not CAPABILITIES["tracemalloc-object-traceback"]:  # pragma: lacks tracemalloc-object-traceback
            patch.setattr("tracemalloc.get_object_traceback", lambda _source: None)
        yield


_HOST_COLLECTION_INTERVAL: Final[int] = 20
_HOST_COLLECTION_DUE: Final = itertools.cycle((False,) * (_HOST_COLLECTION_INTERVAL - 1) + (True,))


@pytest.fixture(autouse=True)
def _collect_garbage() -> Iterator[None]:
    # Multiprocessing objects register finalizers that close pipes, unlink semaphores and write to the resource
    # tracker; when cyclic garbage from one test is collected during a later one, those run against whatever that
    # later test has patched onto os.close or os.write. Collect after every test so finalizers fire in the test that
    # created the objects, before any such patch is in place. This runs after mocker's undo, since autouse fixtures
    # set up first and tear down last. Where a collection runs no finalizers (GraalPy queues them on the host
    # collector), it cannot do that, and a full host collection after every test was nearly all of the suite's runtime.
    # It still needs one now and then: with none at all, GraalPy CI runs stalled past the timeout inside trivial tests.
    yield
    if CAPABILITIES["collected-finalization"] or next(_HOST_COLLECTION_DUE):  # pragma: needs collected-finalization
        gc.collect()


@pytest.fixture
def close_failure(
    mocker: MockerFixture,
) -> Iterator[tuple[Callable[[int], None], OSError, RuntimeError]]:
    locked_fd: int | None = None
    release_error = OSError("release failed")
    release_cause = RuntimeError("release cause")
    release_error.__cause__ = release_cause
    release_error.__suppress_context__ = True
    real_close = os.close

    def capture(fd: int) -> None:
        nonlocal locked_fd
        locked_fd = fd
        mocker.patch("filelock._api.os.close", side_effect=close)

    def close(fd: int) -> None:
        # filelock._api.os is the os module, so this patches os.close process-wide. Raise only for the descriptor
        # under test. An unrelated close inside a GC finalizer would escape as an unraisable exception.
        if fd == locked_fd:
            raise release_error
        real_close(fd)  # pragma: no cover  # only an unrelated close (e.g. a GC finalizer) reaches here

    yield capture, release_error, release_cause
    if locked_fd is not None:  # pragma: no branch  # every consumer calls capture, so this is always set
        real_close(locked_fd)


@pytest.fixture(params=[pytest.param("native", id="native"), pytest.param("no-exchange", id="no-exchange")])
def break_route(request: pytest.FixtureRequest, mocker: MockerFixture) -> None:
    """Run the test with the platform's own stale break, then as where no atomic exchange of two names exists."""
    if request.param == "no-exchange":
        mocker.patch("filelock._util._rename_call", return_value=None)


@pytest.fixture
def on_break_recheck(mocker: MockerFixture) -> Callable[[Path, Callable[[], None]], None]:
    """Schedule an action for right after the stale break re-reads the lock file and before it takes the marker."""

    def schedule(lock: Path, action: Callable[[], None]) -> None:
        alive: Final = filelock._identity.process_alive
        lstat: Final = os.lstat
        state: list[str] = []

        def judged(pid: int) -> bool:
            # The owner check comes right before the break, whose first look at the path is the recheck.
            state.append("judged")
            return alive(pid)

        def lstat_then_act(path: str | os.PathLike[str], *, dir_fd: int | None = None) -> os.stat_result:
            found: Final = lstat(path, dir_fd=dir_fd)
            if state == ["judged"] and os.fspath(path) == os.fspath(lock):
                state.append("acted")
                action()
            return found

        mocker.patch("filelock._identity.process_alive", side_effect=judged)
        mocker.patch.object(os, "lstat", new=lstat_then_act)

    return schedule


@pytest.fixture
def reuse_inode(mocker: MockerFixture) -> Callable[[Path, os.stat_result], None]:  # pragma: win32 no cover
    """
    Stand in for ext4, which hands a freed inode number to the next file created.

    A tmpfs test directory never reuses a number. The stand-in makes ``os.lstat`` report a successor under the number of
    the unlinked file it is given, once no descriptor in this process keeps that inode allocated.
    """

    def stand_in(successor: Path, freed: os.stat_result) -> None:
        created: Final = successor.lstat()
        lstat: Final = os.lstat

        def lstat_with_reuse(path: str | os.PathLike[str], *, dir_fd: int | None = None) -> os.stat_result:
            found: Final = lstat(path, dir_fd=dir_fd)
            if (found.st_dev, found.st_ino) != (created.st_dev, created.st_ino) or _held_open(freed):
                return found
            # The tuple carries the integer fields up to st_size; the float and nanosecond times ride in the mapping.
            extra: Final = {
                name: getattr(found, name)
                for name in dir(found)
                if name.startswith("st_")
                and name not in {"st_mode", "st_ino", "st_dev", "st_nlink", "st_uid", "st_gid", "st_size"}
            }
            return os.stat_result((found.st_mode, freed.st_ino, *found[2:10]), extra)

        mocker.patch("os.lstat", side_effect=lstat_with_reuse)

    return stand_in


def _held_open(node: os.stat_result) -> bool:  # pragma: win32 no cover
    for entry in Path("/dev/fd").iterdir():
        with suppress(OSError):  # the descriptor the listing itself used is closed by now
            if os.path.samestat(os.fstat(int(entry.name)), node):
                return True
    return False


#: Linux and macOS cap PIDs below 2**22 + 1, and Windows answers OpenProcess for it with an invalid-parameter error, so
#: the marker names a holder every platform reads as dead.
DEAD_PID: Final[int] = 2**22 + 1
DEAD_HOLDER: Final[str] = f"{DEAD_PID}\n{host_name()}\n"
DEAD_OWNER: Final[str] = f"filelock/2\npid={DEAD_PID}\nhost={host_name()}\nmode=exclusive\n"

#: Every soft lock that breaks a stale marker, with a record naming a dead owner in the format it reads.
STALE_BREAKERS: Final[pytest.MarkDecorator] = pytest.mark.parametrize(
    ("lock_type", "record"),
    [
        pytest.param(SoftFileLock, DEAD_HOLDER, id="soft"),
        pytest.param(MarkerSoftFileLock, DEAD_OWNER, id="marker"),
        pytest.param(SoftFileLease, DEAD_OWNER, id="lease"),
    ],
)


@pytest.fixture
def working_directories(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    monkeypatch.chdir(first)
    return first, second


@pytest.fixture
def lock_path_in_shared_tmp() -> Iterator[Path]:  # pragma: needs foreign-sticky-tmp
    # Directly in /tmp, the sticky directory every user can create names in, rather than in this test's own directory.
    lock_path = Path("/tmp", f"filelock-{secrets.token_hex(8)}.lock")  # ruff:ignore[hardcoded-temp-file]  # the subject
    yield lock_path
    for derived in (Path(f"{lock_path}.filelock"), Path(f"{lock_path}.rw")):
        shutil.rmtree(derived, ignore_errors=True)
    lock_path.unlink(missing_ok=True)


@pytest.fixture
def directories_of_another_account(mocker: MockerFixture) -> None:  # pragma: needs foreign-sticky-tmp
    # Directories this process creates then look, to the lock, like ones another account created first.
    assert sys.platform != "win32"  # the capability is absent there; narrows os.geteuid for the type checker
    mocker.patch("filelock._util.os.geteuid", autospec=True, return_value=os.geteuid() + 1)


@pytest.fixture
def create_junction() -> Callable[[Path, Path], None]:  # pragma: win32 cover
    # A junction needs no privilege, unlike a symlink, and lstat reports it as a directory.
    def create(link: Path, target: Path) -> None:
        if sys.platform == "win32":  # pragma: no branch  # narrows _winapi for the type checker
            import _winapi

            _winapi.CreateJunction(str(target), str(link))

    return create
