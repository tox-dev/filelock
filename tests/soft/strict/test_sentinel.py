from __future__ import annotations

import os
import re
import signal
import subprocess  # ruff:ignore[suspicious-subprocess-import]  # a finished child yields a PID that no longer runs
import sys
import time
from errno import EACCES, EEXIST, EIO, ENOENT, EWOULDBLOCK
from pathlib import Path
from typing import TYPE_CHECKING, Final

import pytest
from capabilities import CAPABILITIES

from filelock import MarkerSoftFileLock, SoftFileLock, SoftFileLockProtocolError, StrictSoftFileLock, Timeout
from filelock._identity import host_name
from filelock._soft._strict._sentinel import publish_record
from tests.capability_marks import (
    NEEDS_SYMLINK,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from pytest_mock import MockerFixture
from .capability_marks import NEEDS_DIR_FD
from .filesystem_faults import PathValue, patch_strict_clock, route_record_opens_through_os_open

pytestmark: Final[pytest.MarkDecorator] = pytest.mark.requires_hard_links


_NEEDS_FILE_LEASE: Final[pytest.MarkDecorator] = pytest.mark.skipif(
    not CAPABILITIES["file-lease"], reason="leasing the sentinel needs Linux F_SETLEASE on a supported filesystem"
)


_UNPARSABLE_CONTENTS: Final[list[pytest.ParameterSet]] = [
    pytest.param(b"", id="pre-3.22-empty"),
    pytest.param(b"4242\nhost", id="cut"),
]


def test_strict_soft_sentinel_publication_race_acquires_over_strict_winner(
    tmp_path: Path, mocker: MockerFixture
) -> None:
    lock_path = tmp_path / "resource.lock"
    sentinel = b"1\nfilelock-strict-v1\x00\n0\n"
    lock = _race_sentinel_publication(lock_path, mocker, sentinel)

    with lock:
        assert lock.is_locked
    assert lock_path.read_bytes() == sentinel


def _race_sentinel_publication(lock_path: Path, mocker: MockerFixture, sentinel: bytes) -> StrictSoftFileLock:
    real_link = os.link
    raced = False

    def publish_competitor(
        source: PathValue,
        destination: PathValue,
        *,
        src_dir_fd: int | None = None,
        dst_dir_fd: int | None = None,
        follow_symlinks: bool = True,
    ) -> None:
        nonlocal raced  # pragma: no cover  # the declaration has no runtime opcode
        if not raced:
            raced = True
            lock_path.write_bytes(sentinel)
            raise FileExistsError(EEXIST, "sentinel exists")
        real_link(  # pragma: win32 no cover
            source,
            destination,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
            follow_symlinks=follow_symlinks,
        )

    mocker.patch("os.link", side_effect=publish_competitor, autospec=True)
    return StrictSoftFileLock(lock_path, timeout=0)


def test_strict_soft_sentinel_publication_race_times_out_over_legacy_winner(
    tmp_path: Path, mocker: MockerFixture
) -> None:
    lock_path = tmp_path / "resource.lock"
    sentinel = f"{os.getpid()}\n{host_name()}\n".encode()
    lock = _race_sentinel_publication(lock_path, mocker, sentinel)

    with pytest.raises(Timeout):
        lock.acquire()
    assert lock_path.read_bytes() == sentinel


def test_strict_soft_sentinel_uses_unique_private_names(tmp_path: Path, mocker: MockerFixture) -> None:
    lock_path = tmp_path / "resource.lock"
    private_names: list[str] = []

    def reject_publication(source: PathValue, _destination: PathValue, **_options: int | bool | None) -> None:
        private_names.append(Path(os.fsdecode(source)).name)
        raise OSError(EIO, "publication failed")

    mocker.patch("secrets.token_hex", side_effect=["0" * 32, "1" * 32], autospec=True)
    mocker.patch("os.link", side_effect=reject_publication, autospec=True)

    for _attempt in range(2):
        with pytest.raises(OSError, match="publication failed"):
            StrictSoftFileLock(lock_path).acquire()
    assert private_names == [
        ".resource.lock.private-v1-00000000000000000000000000000000.tmp",
        ".resource.lock.private-v1-11111111111111111111111111111111.tmp",
    ]


@pytest.mark.parametrize(
    ("make_occupant", "found"),
    [
        pytest.param(Path.mkdir, "a directory", id="directory"),
        pytest.param(
            lambda path: path.symlink_to(path.parent / "missing"),
            "a symlink",
            id="symlink",
            marks=NEEDS_SYMLINK,
        ),
        pytest.param(
            getattr(os, "mkfifo", None),
            "a special file",
            id="fifo",
            marks=pytest.mark.skipif(not CAPABILITIES["fifo"], reason="needs os.mkfifo"),
        ),
        pytest.param(lambda path: path.write_bytes(b"x" * 4096), "a file it cannot read", id="oversized"),
    ],
)
def test_strict_soft_damaged_lock_path_names_occupant(
    tmp_path: Path, make_occupant: Callable[[Path], object], found: str
) -> None:
    lock_path = tmp_path / "resource.lock"
    make_occupant(lock_path)
    lock = StrictSoftFileLock(lock_path)

    with pytest.raises(
        SoftFileLockProtocolError, match=f"expected the strict sentinel at the lock path, found {found}"
    ):
        lock.acquire()
    assert (lock.lock_path_occupant or "").startswith(found)


@pytest.mark.parametrize("content", _UNPARSABLE_CONTENTS)
def test_strict_soft_unparsable_lock_path_blocks(tmp_path: Path, content: bytes) -> None:
    lock_path = tmp_path / "resource.lock"
    lock_path.write_bytes(content)
    lock = StrictSoftFileLock(lock_path, timeout=0)

    with pytest.raises(Timeout):
        lock.acquire()
    assert (lock.lock_path_occupant, lock_path.read_bytes()) == (
        "a file that is neither the sentinel nor a soft-lock marker it can parse",
        content,
    )


@pytest.mark.parametrize("content", _UNPARSABLE_CONTENTS)
def test_strict_soft_unparsable_lock_path_raises_once_unchanged(
    tmp_path: Path, mocker: MockerFixture, content: bytes
) -> None:
    lock_path = tmp_path / "resource.lock"
    lock_path.write_bytes(content)
    patch_strict_clock(mocker, "_lock", monotonic=mocker.create_autospec(time.monotonic, side_effect=[0.0, 2.0]))
    lock = StrictSoftFileLock(lock_path, poll_interval=0)

    with pytest.raises(SoftFileLockProtocolError, match="marker it can parse, unchanged for 2 seconds"):
        lock.acquire()
    assert lock_path.read_bytes() == content


def test_strict_soft_unreadable_lock_path_blocks(tmp_path: Path, mocker: MockerFixture) -> None:
    lock_path = tmp_path / "resource.lock"
    lock_path.write_bytes(b"")
    real_open = os.open

    def sharing_violation(path: PathValue, flags: int, mode: int = 0o777, *, dir_fd: int | None = None) -> int:
        # Windows refuses to open a marker its legacy holder keeps open.
        if Path(os.fsdecode(path)).name == lock_path.name:
            raise PermissionError(EACCES, "sharing violation")
        return real_open(path, flags, mode, dir_fd=dir_fd)  # pragma: no cover  # an occupied path publishes nothing

    route_record_opens_through_os_open(mocker)
    mocker.patch("os.open", autospec=True, side_effect=sharing_violation)
    lock = StrictSoftFileLock(lock_path, timeout=0)

    with pytest.raises(Timeout):
        lock.acquire()
    assert lock.lock_path_occupant == "a file it cannot read (sharing violation)"


def test_strict_soft_answers_a_sentinel_break(initialized_lock_path: Path, mocker: MockerFixture) -> None:
    real_open = os.open
    breaking = True

    def answer_after_one_open(path: PathValue, flags: int, mode: int = 0o777, *, dir_fd: int | None = None) -> int:
        nonlocal breaking
        if breaking and Path(os.fsdecode(path)).name == initialized_lock_path.name:
            breaking = False
            raise BlockingIOError(EWOULDBLOCK, "break in progress")
        return real_open(path, flags, mode, dir_fd=dir_fd)

    route_record_opens_through_os_open(mocker, claims=False)
    mocker.patch("os.open", autospec=True, side_effect=answer_after_one_open)

    with StrictSoftFileLock(initialized_lock_path, timeout=0) as lock:
        assert (breaking, lock.is_locked) == (False, True)


def test_strict_soft_unanswered_sentinel_break_is_contention(
    initialized_lock_path: Path, mocker: MockerFixture
) -> None:
    real_open = os.open
    now = 0.0

    def never_answered(path: PathValue, flags: int, mode: int = 0o777, *, dir_fd: int | None = None) -> int:
        if Path(os.fsdecode(path)).name == initialized_lock_path.name:
            raise BlockingIOError(EWOULDBLOCK, "break in progress")
        return real_open(path, flags, mode, dir_fd=dir_fd)  # pragma: no cover  # an occupied path publishes nothing

    def tick(seconds: float) -> None:
        nonlocal now
        now += seconds

    patch_strict_clock(mocker, "_sentinel", monotonic=lambda: now, sleep=tick)
    route_record_opens_through_os_open(mocker)
    mocker.patch("os.open", autospec=True, side_effect=never_answered)
    lock = StrictSoftFileLock(initialized_lock_path, timeout=0)

    with pytest.raises(Timeout):
        lock.acquire()
    assert (lock.lock_path_occupant, 0.5 <= now < 1) == ("a file it cannot read (break in progress)", True)


@pytest.fixture
def leased_sentinel(initialized_lock_path: Path) -> Iterator[Path]:  # pragma: needs file-lease
    assert sys.platform != "win32"
    import fcntl

    previous_handler = signal.signal(signal.SIGIO, signal.SIG_IGN)
    lease_fd = os.open(initialized_lock_path, os.O_RDWR)
    try:
        fcntl.fcntl(lease_fd, fcntl.F_SETLEASE, fcntl.F_WRLCK)
        yield initialized_lock_path
    finally:
        os.close(lease_fd)
        signal.signal(signal.SIGIO, previous_handler)


@_NEEDS_FILE_LEASE  # pragma: needs file-lease
@pytest.mark.parametrize(
    ("timeout", "blocking"), [pytest.param(0.1, True, id="timeout"), pytest.param(-1, False, id="non-blocking")]
)
def test_strict_soft_lease_on_sentinel_keeps_deadline(leased_sentinel: Path, timeout: float, *, blocking: bool) -> None:
    lock = StrictSoftFileLock(leased_sentinel)

    started = time.monotonic()
    with pytest.raises(Timeout):
        lock.acquire(timeout=timeout, blocking=blocking)
    assert time.monotonic() - started < 5


def test_strict_soft_exited_soft_lock_owner_raises_without_waiting(tmp_path: Path, mocker: MockerFixture) -> None:
    lock_path = tmp_path / "resource.lock"
    pid = subprocess.run(
        [sys.executable, "-c", "import os; print(os.getpid())"], check=True, capture_output=True, text=True
    ).stdout.strip()
    lock_path.write_text(f"{pid}\n{host_name()}\n", encoding="ascii")
    found = f"a soft-lock marker left by pid {pid} on {host_name()}, which has exited"
    sleep = mocker.create_autospec(time.sleep)
    patch_strict_clock(mocker, "_sentinel", sleep=sleep)
    lock = StrictSoftFileLock(lock_path)

    with pytest.raises(SoftFileLockProtocolError, match=re.escape(f"found {found}")):
        lock.acquire()
    assert (sleep.call_count, lock.lock_path_occupant, lock_path.exists()) == (0, found, True)


@pytest.mark.parametrize(
    "legacy", [pytest.param(SoftFileLock, id="soft"), pytest.param(MarkerSoftFileLock, id="marker")]
)
def test_strict_soft_live_soft_lock_owner_blocks(tmp_path: Path, legacy: type[SoftFileLock]) -> None:
    lock_path = tmp_path / "resource.lock"
    with legacy(lock_path):
        lock = StrictSoftFileLock(lock_path, timeout=0)
        with pytest.raises(Timeout):
            lock.acquire()
        assert (lock.claims, lock.lock_path_occupant) == (
            (),
            f"a soft-lock marker held by pid {os.getpid()} on {host_name()}",
        )


def test_strict_soft_lock_path_occupant_escapes_a_hostname_outside_the_grammar(tmp_path: Path) -> None:
    lock_path = tmp_path / "resource.lock"
    lock_path.write_text(f"{os.getpid()}\nother\x1b[2K\x1b[1A\x7fhost\n", encoding="utf-8")
    occupant = StrictSoftFileLock(lock_path).lock_path_occupant
    assert occupant == f"a soft-lock marker held by pid {os.getpid()} on other?1b[2K?1b[1A?7fhost"


def test_strict_soft_lock_path_occupant_is_none_for_the_sentinel(tmp_path: Path) -> None:
    lock = StrictSoftFileLock(tmp_path / "resource.lock")
    before = lock.lock_path_occupant
    with lock:
        assert (before, lock.lock_path_occupant) == (None, None)


def test_strict_soft_sentinel_removed_after_publication_backs_off(tmp_path: Path, mocker: MockerFixture) -> None:
    lock_path = tmp_path / "resource.lock"
    real_publish = publish_record

    def legacy_break_lock_after_publication(public_path: str, record: bytes, mode: int) -> BaseException | None:
        # With timeout=0 the acquire gives up before any claim, so the sentinel is the only record published.
        cleanup_error = real_publish(public_path, record, mode)
        Path(public_path).unlink()
        return cleanup_error

    mocker.patch(
        "filelock._soft._strict._sentinel.publish_record",
        autospec=True,
        side_effect=legacy_break_lock_after_publication,
    )
    lock = StrictSoftFileLock(lock_path, timeout=0)

    with pytest.raises(Timeout):
        lock.acquire()
    assert (lock.is_locked, lock_path.exists()) == (False, False)


@pytest.mark.skipif(  # pragma: win32 no cover
    sys.platform == "win32", reason="the test routes claim reads through os.open without delete sharing"
)
def test_strict_soft_sentinel_vanishing_before_open_is_republished(
    initialized_lock_path: Path, mocker: MockerFixture
) -> None:
    real_open = os.open
    vanished = False

    def vanish_once(path: PathValue, flags: int, mode: int = 0o777, *, dir_fd: int | None = None) -> int:
        # A legacy break_lock() removes the sentinel between the inspection's lstat and its open.
        nonlocal vanished
        if not vanished and Path(os.fsdecode(path)).name == initialized_lock_path.name:
            vanished = True
            raise FileNotFoundError(ENOENT, "sentinel removed", os.fsdecode(path))
        return real_open(path, flags, mode, dir_fd=dir_fd)

    route_record_opens_through_os_open(mocker)
    mocker.patch("os.open", autospec=True, side_effect=vanish_once)

    with StrictSoftFileLock(initialized_lock_path, timeout=0) as lock:
        assert (vanished, lock.is_locked) == (True, True)


def test_strict_soft_rides_out_a_transient_non_sentinel(tmp_path: Path, mocker: MockerFixture) -> None:
    lock_path = tmp_path / "resource.lock"
    lock_path.mkdir()
    patch_strict_clock(
        mocker, "_sentinel", sleep=mocker.create_autospec(time.sleep, side_effect=lambda _seconds: lock_path.rmdir())
    )

    with StrictSoftFileLock(lock_path, timeout=0) as lock:
        assert lock.is_locked


def test_strict_soft_sentinel_race_with_non_regular_winner_blocks(tmp_path: Path, mocker: MockerFixture) -> None:
    lock_path = tmp_path / "resource.lock"

    def publish_directory(
        _source: PathValue,
        _destination: PathValue,
        **_options: int | bool | None,
    ) -> None:
        lock_path.mkdir()
        raise FileExistsError(EEXIST, "sentinel exists")

    mocker.patch("os.link", side_effect=publish_directory, autospec=True)

    with pytest.raises(SoftFileLockProtocolError, match="found a directory"):
        StrictSoftFileLock(lock_path, timeout=0).acquire()
    assert lock_path.is_dir()


@NEEDS_DIR_FD  # pragma: needs dir-fd
def test_strict_soft_sentinel_remains_after_private_cleanup_failure(tmp_path: Path, mocker: MockerFixture) -> None:
    lock_path = tmp_path / "resource.lock"
    real_unlink = os.unlink
    failed = False

    def fail_first_private_cleanup(path: PathValue, *, dir_fd: int | None = None) -> None:
        nonlocal failed
        if not failed and Path(os.fsdecode(path)).name.startswith(".resource.lock.private-"):
            failed = True
            raise OSError(EIO, "private cleanup failed")
        real_unlink(path, dir_fd=dir_fd)

    mocker.patch("os.unlink", side_effect=fail_first_private_cleanup, autospec=True)

    with pytest.raises(OSError, match="private cleanup failed"):
        StrictSoftFileLock(lock_path).acquire()
    with pytest.raises(Timeout):
        SoftFileLock(lock_path, timeout=0).acquire()
    with StrictSoftFileLock(lock_path, timeout=0) as strict:
        assert (lock_path.read_bytes(), strict.is_locked) == (b"1\nfilelock-strict-v1\x00\n0\n", True)
