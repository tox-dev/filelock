from __future__ import annotations

import gc
import itertools
import os
import secrets
import shutil
import subprocess  # ruff:ignore[suspicious-subprocess-import]  # only the fixed-argument, no-shell run below
import sys
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import TYPE_CHECKING, Final, Protocol, TypeVar

import pytest
from capabilities import CAPABILITIES

from filelock import BaseFileLock, MarkerSoftFileLock, SoftFileLease, SoftFileLock, Timeout
from filelock._identity import host_name, process_alive
from tests.descriptor_helpers import descriptors_on
from tests.fork_helpers import exit_child

if TYPE_CHECKING:
    from collections.abc import Callable, Generator, Iterator

    from pytest_mock import MockerFixture

# Coverage restarts in every patched subprocess and re-imports the plugin there; pytest's pythonpath reaches only
# this process, so export it.
os.environ["PYTHONPATH"] = os.pathsep.join(
    part for part in (str(Path(__file__).parent.parent / "tasks"), os.environ.get("PYTHONPATH")) if part
)

_LockT = TypeVar("_LockT", bound=BaseFileLock)


def pytest_configure(config: pytest.Config) -> None:
    if sys.implementation.name == "graalpy":  # pragma: no cover  # only GraalPy's semaphore finalizer raises this
        # GraalPy keeps a fork-context semaphore's name after the immediate unlink, so its finalizer unlinks it again
        # and fails with ENOENT. The warning text carries the traceback, so only that cleanup's ENOENT is ignored.
        # Upstream: https://github.com/oracle/graalpython/issues/1316
        config.addinivalue_line(
            "filterwarnings",
            "ignore:(?s)Exception ignored in.+<Finalize object, dead>.+ in _cleanup\\n.+FileNotFoundError"
            ":pytest.PytestUnraisableExceptionWarning",
        )


@contextmanager
def held_after_reclaim(lock: _LockT) -> Generator[_LockT]:
    # Hold *lock* through :func:`acquire_after_reclaim` and release it on exit.
    acquire_after_reclaim(lock)
    try:
        yield lock
    finally:
        lock.release()


def acquire_after_reclaim(lock: BaseFileLock) -> None:
    # Take a path whose stale marker the lock has to break first, without a wall-clock budget.
    #
    # A try finds the marker and breaks it, or first watches an unreadable one age; the last creates its own. A deadline
    # would test how fast the machine is: a first try that outlasts it times out after the marker is gone and before the
    # lock is held.
    for _ in range(3):
        with suppress(Timeout):
            lock.acquire(blocking=False)
        if lock.is_locked:
            return


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    # Termux builds can omit os.link. Fallback tests still run there.
    if not hasattr(os, "link"):  # pragma: no cover  # the body runs only on Termux/Android, absent from the CI matrix
        skip = pytest.mark.skip(reason="test requires os.link (hard links), absent on Termux/Android")
        for item in items:
            if item.get_closest_marker("requires_hard_links") is not None:
                item.add_marker(skip)


# Captured at import, before any test forks, so a fork child can tell it is not the session.
_SESSION_PID: Final[int] = os.getpid()


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo[None]) -> None:  # pragma: needs fork
    if (pid := os.getpid()) != _SESSION_PID:
        os.write(2, f"fork child {pid} escaped into pytest at {item.nodeid} ({call.when})\n".encode())
        exit_child(70)


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
_HOST_COLLECTION_DUE: Final[Iterator[bool]] = itertools.cycle((False,) * (_HOST_COLLECTION_INTERVAL - 1) + (True,))


@pytest.fixture(autouse=True)
def _collect_garbage() -> Iterator[None]:
    # Multiprocessing objects register finalizers that close pipes, unlink semaphores and write to the resource tracker;
    # when cyclic garbage from one test is collected during a later one, those run against whatever that later test has
    # patched onto os.close or os.write. Collect after every test so finalizers fire in the test that created the
    # objects, before any such patch is in place.
    #
    # This runs after mocker's undo, since autouse fixtures set up first and tear down last. Where a collection runs no
    # finalizers (GraalPy queues them on the host collector), it cannot do that, and a full host collection after every
    # test was nearly all of the suite's runtime. It still needs one now and then: with none at all, GraalPy CI runs
    # stalled past the timeout inside trivial tests.
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
        mocker.patch("os.close", side_effect=close, autospec=True)

    def close(fd: int) -> None:
        # GC finalizers can close unrelated descriptors while the patch is active.
        if fd == locked_fd:
            raise release_error
        real_close(fd)  # pragma: no cover  # only an unrelated close (e.g. a GC finalizer) reaches here

    yield capture, release_error, release_cause
    if locked_fd is not None:  # pragma: no branch  # every consumer calls capture, so this is always set
        real_close(locked_fd)


@pytest.fixture(params=[pytest.param("native", id="native"), pytest.param("no-exchange", id="no-exchange")])
def break_route(request: pytest.FixtureRequest, mocker: MockerFixture) -> None:
    # Run the test with the platform's own stale break, then as where no atomic exchange of two names exists.
    if request.param == "no-exchange":
        mocker.patch("filelock._filesystem._exchange._rename_call", return_value=None, autospec=True)


@pytest.fixture
def on_break_recheck(mocker: MockerFixture) -> Callable[[Path, Callable[[], None]], None]:
    # Schedule an action for right after the stale break re-reads the lock file and before it takes the marker.
    def schedule(lock: Path, action: Callable[[], None]) -> None:
        lstat = os.lstat
        state: list[str] = []

        def judged(pid: int) -> bool:
            # The owner check comes right before the break, whose first look at the path is the recheck.
            state.append("judged")
            return process_alive(pid)

        def lstat_then_act(path: str | os.PathLike[str], *, dir_fd: int | None = None) -> os.stat_result:
            found: Final[os.stat_result] = lstat(path, dir_fd=dir_fd)
            if state == ["judged"] and os.fspath(path) == os.fspath(lock):
                state.append("acted")
                action()
            return found

        mocker.patch("filelock._identity._process.process_alive", side_effect=judged, autospec=True)
        mocker.patch.object(os, "lstat", new=lstat_then_act)

    return schedule


@pytest.fixture
def reuse_inode(mocker: MockerFixture) -> Callable[[Path, os.stat_result], None]:  # pragma: win32 no cover
    # Stand in for ext4, which hands a freed inode number to the next file created.
    #
    # A tmpfs test directory never reuses a number. The stand-in makes ``os.lstat`` report a successor under the number
    # of the unlinked file it is given, once no descriptor in this process keeps that inode allocated.
    def stand_in(successor: Path, freed: os.stat_result) -> None:
        created: Final[os.stat_result] = successor.lstat()
        lstat = os.lstat

        def lstat_with_reuse(path: str | os.PathLike[str], *, dir_fd: int | None = None) -> os.stat_result:
            found: Final[os.stat_result] = lstat(path, dir_fd=dir_fd)
            if (found.st_dev, found.st_ino) != (created.st_dev, created.st_ino) or descriptors_on(freed):
                return found
            # The tuple carries the integer fields up to st_size; the float and nanosecond times ride in the mapping.
            extra = {
                name: getattr(found, name)
                for name in dir(found)
                if name.startswith("st_")
                and name not in {"st_mode", "st_ino", "st_dev", "st_nlink", "st_uid", "st_gid", "st_size"}
            }
            return os.stat_result((found.st_mode, freed.st_ino, *found[2:10]), extra)

        mocker.patch("os.lstat", side_effect=lstat_with_reuse, autospec=True)

    return stand_in


#: Linux and macOS cap PIDs below 2**22 + 1, and Windows answers OpenProcess for it with an invalid-parameter error, so
#: the marker names a holder every platform reads as dead.
_DEAD_PID: Final[int] = 2**22 + 1
DEAD_HOLDER: Final[str] = f"{_DEAD_PID}\n{host_name()}\n"
DEAD_OWNER: Final[str] = f"filelock/2\npid={_DEAD_PID}\nhost={host_name()}\nmode=exclusive\n"

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
    # /tmp itself, where every account can create names, rather than this test's own directory.
    lock_path = Path("/tmp", f"filelock-{secrets.token_hex(8)}.lock")  # ruff:ignore[hardcoded-temp-file]  # the subject
    yield lock_path
    for derived in (Path(f"{lock_path}.filelock"), Path(f"{lock_path}.rw")):
        shutil.rmtree(derived, ignore_errors=True)
    lock_path.unlink(missing_ok=True)


@pytest.fixture
def directories_of_another_account(mocker: MockerFixture) -> None:  # pragma: needs foreign-sticky-tmp
    # Directories this process creates then look, to the lock, like ones another account created first.
    assert sys.platform != "win32"  # the capability is absent there; narrows os.geteuid for the type checker
    mocker.patch("filelock._filesystem._files.os.geteuid", autospec=True, return_value=os.geteuid() + 1)


@pytest.fixture  # pragma: darwin no cover
def uid_shared_with_unmapped_accounts(mocker: MockerFixture) -> None:  # pragma: needs foreign-sticky-tmp
    assert sys.platform != "win32"  # narrows os.geteuid for the type checker

    def read_proc(path: Path, **_: str | None) -> str:
        assert path == Path("/proc/sys/kernel/overflowuid")
        return str(os.geteuid())

    mocker.patch("filelock._filesystem._ownership._linux_owner_probe", autospec=True, return_value=lambda _fd: False)
    mocker.patch.object(Path, "read_text", autospec=True, side_effect=read_proc)
    mocker.patch.object(Path, "read_bytes", autospec=True, return_value=b"0 100000 65536")


@pytest.fixture  # pragma: win32 no cover  # pragma: darwin no cover
def sticky_dir_with_mapped_owner(tmp_path: Path, mocker: MockerFixture) -> Path:
    assert sys.platform != "win32"
    tmp_path.chmod(0o1700)
    mocker.patch("filelock._filesystem._ownership._linux_owner_probe", autospec=True, return_value=None)
    sysctl = tmp_path / "overflowuid"
    sysctl.write_text(str(os.geteuid()), encoding="ascii")
    proc = tmp_path / "proc"
    proc.mkdir()
    (proc / "uid_map").write_bytes(b"0 0 4294967295")
    (proc / "mountinfo").write_bytes(b"18 4 0:3 / /tmp rw - tmpfs tmpfs rw")
    read_bytes = Path.read_bytes

    def read_fd_info(path: Path) -> bytes:
        if path.parent == proc / "fdinfo":
            os.fstat(int(path.name))
            return b"mnt_id:\t18"
        return read_bytes(path)

    mocker.patch.object(Path, "read_bytes", autospec=True, side_effect=read_fd_info)
    paths = {"/proc/thread-self": proc, "/proc/sys/kernel/overflowuid": sysctl}
    mocker.patch(
        "filelock._filesystem._ownership.Path",
        autospec=True,
        side_effect=lambda name: paths.get(name, Path(name)),
    )
    return tmp_path


@pytest.fixture
def create_junction() -> Callable[[Path, Path], None]:  # pragma: win32 cover
    # A junction needs no privilege, unlike a symlink, and lstat reports it as a directory.
    def create(link: Path, target: Path) -> None:
        if sys.platform == "win32":  # pragma: no branch  # narrows _winapi for the type checker
            import _winapi

            _winapi.CreateJunction(str(target), str(link))

    return create


@pytest.fixture
def inheriting_directory(tmp_path: Path) -> Path:  # pragma: win32 cover
    # A directory without inheritable entries gives a new file the token's default permissions, which icacls shows
    # like explicit ones; an inheritable entry for Users makes inherited entries show as (I).
    directory = tmp_path / "inheriting"
    directory.mkdir()
    subprocess.run(
        [Path(os.environ["SYSTEMROOT"], "System32", "icacls.exe"), directory, "/grant", "*S-1-5-32-545:(OI)(CI)(RX)"],
        capture_output=True,
        check=True,
    )
    return directory


@pytest.fixture
def hold_oplock() -> Iterator[OplockHolder]:  # pragma: win32 cover
    # The marker skips the run off Windows; this only narrows ctypes.WinDLL for ty.
    assert sys.platform == "win32"
    import ctypes
    import struct
    from ctypes import wintypes

    class Overlapped(ctypes.Structure):
        _fields_ = (
            ("Internal", ctypes.c_void_p),
            ("InternalHigh", ctypes.c_void_p),
            ("Offset", wintypes.DWORD),
            ("OffsetHigh", wintypes.DWORD),
            ("hEvent", wintypes.HANDLE),
        )

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.restype = kernel32.CreateEventW.restype = wintypes.HANDLE
    kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    kernel32.DeviceIoControl.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.c_void_p,
        ctypes.POINTER(Overlapped),
    ]
    closers: list[Callable[[], None]] = []
    answers: list[ctypes.Array[ctypes.c_char]] = []  # Windows writes one when its request completes

    def hold(path: Path, *, read_handle: bool = False) -> Callable[[], None]:
        generic_read, share_all, open_existing, flag_overlapped = 0x80000000, 0x1 | 0x2 | 0x4, 3, 0x40000000
        handle = kernel32.CreateFileW(str(path), generic_read, share_all, None, open_existing, flag_overlapped, None)
        overlapped = Overlapped(hEvent=kernel32.CreateEventW(None, True, False, None))
        # FSCTL_REQUEST_OPLOCK_LEVEL_1 stays pending while granted; a read handle suffices to make other opens wait.
        request: tuple[int, bytes | None, int, ctypes.Array[ctypes.c_char] | None, int] = (0x00090000, None, 0, None, 0)
        if read_handle:
            # FSCTL_REQUEST_OPLOCK with a version 1 REQUEST_OPLOCK_INPUT_BUFFER of 12 bytes that requests
            # OPLOCK_LEVEL_CACHE_READ | OPLOCK_LEVEL_CACHE_HANDLE. Opens that share everything leave this oplock alone,
            # and a rename waits for its holder. The answer is a REQUEST_OPLOCK_OUTPUT_BUFFER.
            asked = struct.pack("<HHII", 1, 12, 0x1 | 0x2, 0x1)
            answers.append(answer := ctypes.create_string_buffer(24))
            request = (0x00090240, asked, len(asked), answer, len(answer))
        assert not kernel32.DeviceIoControl(handle, *request, None, ctypes.byref(overlapped))
        assert ctypes.get_last_error() == 997  # ERROR_IO_PENDING
        closed = False

        def close() -> None:
            nonlocal closed
            if closed:
                return
            closed = True
            kernel32.CloseHandle(handle)
            # The request completes on close and writes overlapped, so wait before freeing it.
            kernel32.WaitForSingleObject(overlapped.hEvent, 5000)
            kernel32.CloseHandle(overlapped.hEvent)

        closers.append(close)
        return close

    yield hold
    for close in closers:
        close()


class OplockHolder(Protocol):
    def __call__(self, path: Path, *, read_handle: bool = False) -> Callable[[], None]: ...


@pytest.fixture
def oplocked_lock_path(
    request: pytest.FixtureRequest, tmp_path: Path, hold_oplock: Callable[[Path], Callable[[], None]]
) -> Path:  # pragma: win32 cover
    lock_path = tmp_path / "a"
    lock_path.write_text(getattr(request, "param", ""), encoding="utf-8")
    hold_oplock(lock_path)
    return lock_path


@pytest.fixture
def soft_create_path() -> str:
    return (
        "filelock._soft._exclusive._publication.create_exclusive_non_reparse_fd"
        if sys.platform == "win32"
        else "os.open"
    )
