from __future__ import annotations

import gc
import itertools
import os
import socket
import sys
import threading
import time
from contextlib import contextmanager, suppress
from errno import EACCES, EINTR, EINVAL, EIO, ENODEV, ENOSPC, EPERM
from multiprocessing import get_context
from pathlib import Path
from typing import TYPE_CHECKING, Final

import pytest
from capabilities import CAPABILITIES

import filelock._soft
from filelock import CloseErrorPolicy, MarkerSoftFileLock, SoftFileLease, SoftFileLock, Timeout
from filelock._identity import host_name, process_start_token
from filelock._soft import _MAX_LOCK_FILE_SIZE, MALFORMED_LOCK_AGE_THRESHOLD, _begin_claim, _end_claim, _read_lock_file
from filelock._soft_protocol import STRICT_SOFT_SENTINEL_RECORD
from filelock._util import exchange_names
from tests.capability_marks import NEEDS_POSIX_SIGNALS, NEEDS_STALE_RECLAIM, NEEDS_SYMLINK, NEEDS_UNLINK_OPEN_FILE
from tests.conftest import DEAD_OWNER, STALE_BREAKERS, acquire_after_reclaim, held_after_reclaim
from tests.pid_namespace_helpers import pin_pid_namespace
from tests.process_helpers import break_then_hang_after_exchange, call_within, cleanup_processes

if TYPE_CHECKING:
    from collections.abc import Callable, Generator
    from unittest.mock import MagicMock

    from pytest_mock import MockerFixture

_WINDOWS_ONLY: Final[pytest.MarkDecorator] = pytest.mark.skipif(sys.platform != "win32", reason="windows-only")

_HOST: Final[str] = host_name()
_DEAD_PID: Final[int] = 2**22 + 1
_WIN_ERROR_ACCESS_DENIED: Final[int] = 5
_WIN_ERROR_INVALID_PARAMETER: Final[int] = 87
#: A spawned GraalPy interpreter alone can take seconds to start on a loaded runner.
_PROCESS_DEADLINE: Final[int] = 30


@pytest.fixture
def lock_path(tmp_path: Path) -> Path:
    return tmp_path / "test.lock"


def _holder(pid: int, *, host: str = _HOST, start: int | None = None) -> str:
    return "\n".join([str(pid), host, *([] if start is None else [str(start)])]) + "\n"


def test_lock_writes_pid_and_hostname(lock_path: Path) -> None:
    with SoftFileLock(lock_path):
        lines = lock_path.read_text(encoding="utf-8").strip().splitlines()
        assert lines[:2] == [str(os.getpid()), _HOST]
        # Every platform with a proven start time writes it as an integer third line; one without writes two lines.
        if (token := process_start_token(os.getpid())) is not None:
            assert lines == [str(os.getpid()), _HOST, str(token)]
        else:  # pragma: no cover  # every CI platform exposes a start time
            assert lines == [str(os.getpid()), _HOST]


@pytest.mark.parametrize(
    "content",
    [
        pytest.param(_holder(_DEAD_PID), id="two_line"),
        pytest.param(_holder(_DEAD_PID, start=123456789), id="three_line"),
    ],
)
@NEEDS_STALE_RECLAIM
def test_stale_lock_broken_when_process_dead(lock_path: Path, mocker: MockerFixture, content: str) -> None:
    lock_path.write_text(content, encoding="utf-8")
    mocker.patch("filelock._identity.process_alive", return_value=False)
    _assert_self_heals(lock_path)


def test_stale_lock_not_broken_when_process_alive(lock_path: Path, mocker: MockerFixture) -> None:
    lock_path.write_text(_holder(os.getpid()), encoding="utf-8")
    mocker.patch("filelock._identity.process_alive", return_value=True)
    _assert_times_out(lock_path)


def test_stale_lock_not_broken_different_hostname(lock_path: Path) -> None:
    lock_path.write_text(_holder(_DEAD_PID, host="other-host.example.com"), encoding="utf-8")
    _assert_times_out(lock_path)


@pytest.mark.parametrize(
    ("lock_type", "record"),
    [
        pytest.param(SoftFileLock, _holder(_DEAD_PID, host="other-host.example.com"), id="soft"),
        pytest.param(MarkerSoftFileLock, _holder(_DEAD_PID, host="other-host.example.com"), id="marker-v1"),
        pytest.param(
            MarkerSoftFileLock,
            f"filelock/2\npid={_DEAD_PID}\nhost=other-host.example.com\nmode=exclusive\n",
            id="marker-v2",
        ),
    ],
)
def test_timeout_names_a_holder_on_another_host(lock_path: Path, lock_type: type[SoftFileLock], record: str) -> None:
    lock_path.write_text(record, encoding="utf-8")
    with pytest.raises(Timeout, match=f"held by pid {_DEAD_PID} on other-host.example.com, which this host cannot"):
        lock_type(lock_path, timeout=0).acquire()


def test_timeout_leaves_out_a_holder_on_this_host(lock_path: Path) -> None:
    with SoftFileLock(lock_path), pytest.raises(Timeout) as caught:
        SoftFileLock(lock_path, timeout=0).acquire()
    assert str(caught.value) == f"The file lock '{lock_path}' could not be acquired."


_REQUIRES_START_TOKEN: Final[pytest.MarkDecorator] = pytest.mark.skipif(
    process_start_token(os.getpid()) is None, reason="platform exposes no proven process start time"
)


@_REQUIRES_START_TOKEN
def test_recycled_pid_marker_self_heals(lock_path: Path) -> None:
    # A live PID (our own) whose recorded start token differs is a recycled PID: the process that wrote the marker is
    # gone even though the number is now in use. No mock: the real start token is read and found not to match.
    token = process_start_token(os.getpid())
    assert token is not None
    lock_path.write_text(_holder(os.getpid(), start=token + 1), encoding="utf-8")
    _assert_self_heals(lock_path)


@_REQUIRES_START_TOKEN
@pytest.mark.parametrize(
    ("contender_namespace", "reclaimed"),
    [pytest.param(0xF0000001, True, id="same-namespace"), pytest.param(0xF0000002, False, id="sibling-namespace")],
)
def test_recycled_pid_marker_self_heals_only_in_its_pid_namespace(
    lock_path: Path, mocker: MockerFixture, contender_namespace: int, *, reclaimed: bool
) -> None:
    token: Final = process_start_token(os.getpid())
    assert token is not None
    pin_pid_namespace(mocker, 0xF0000001)
    lock_path.write_text(_holder(os.getpid(), host=host_name(), start=token + 1), encoding="utf-8")
    pin_pid_namespace(mocker, contender_namespace)

    lock: Final[SoftFileLock] = SoftFileLock(lock_path)
    acquire_after_reclaim(lock)
    assert lock.is_locked is reclaimed
    lock.release()


@_REQUIRES_START_TOKEN
def test_live_process_with_matching_start_token_retained(lock_path: Path) -> None:
    # Our own PID and our own start token: the recorded owner is this live process, so the marker must stand.
    lock_path.write_text(_holder(os.getpid(), start=process_start_token(os.getpid())), encoding="utf-8")
    _assert_times_out(lock_path)


def test_live_process_without_start_token_falls_back_to_pid(lock_path: Path) -> None:
    # A two-line marker carries no start token, so liveness rests on the PID alone; a live PID is left held.
    lock_path.write_text(_holder(os.getpid()), encoding="utf-8")
    _assert_times_out(lock_path)


@NEEDS_POSIX_SIGNALS  # pragma: needs posix-signals
@pytest.mark.parametrize(
    "errno",
    [pytest.param(EPERM, id="eperm"), pytest.param(ENODEV, id="unexpected_device")],
)
def test_stale_lock_not_broken_on_kill_error(lock_path: Path, mocker: MockerFixture, errno: int) -> None:
    lock_path.write_text(_holder(99999), encoding="utf-8")
    mocker.patch("filelock._identity.os.kill", side_effect=OSError(errno, "kill failed"))
    _assert_times_out(lock_path)


@pytest.mark.usefixtures("unchanged_past_grace")
@pytest.mark.parametrize("lock_type", [SoftFileLock, MarkerSoftFileLock, SoftFileLease])
def test_empty_lock_not_evicted_when_old(lock_path: Path, lock_type: type[SoftFileLock]) -> None:
    # A SoftFileLock before 3.22 writes nothing into its marker and holds it for as long as it runs.
    lock_path.write_bytes(b"")
    contender: Final = lock_type(lock_path)
    for _ in range(2):
        with pytest.raises(Timeout):
            contender.acquire(blocking=False)
    assert lock_path.read_bytes() == b""


@pytest.mark.skipif(sys.platform == "win32", reason="Windows cannot exchange two names")
@STALE_BREAKERS
@NEEDS_STALE_RECLAIM
def test_stale_break_restores_a_stranded_empty_marker(  # pragma: win32 no cover
    tmp_path: Path, lock_type: type[SoftFileLock], record: str
) -> None:
    (lock := tmp_path / "test.lock").write_text(record, encoding="utf-8")
    _break_name(lock).write_bytes(b"")
    with pytest.raises(Timeout):
        lock_type(lock).acquire(blocking=False)

    assert (lock.read_bytes(), list(tmp_path.glob("test.lock.break.*"))) == (b"", [])


def test_timeout_names_an_owner_that_recorded_no_pid(lock_path: Path) -> None:
    lock_path.write_bytes(b"")
    with pytest.raises(Timeout, match="held by an owner that recorded no PID"):
        SoftFileLock(lock_path).acquire(blocking=False)


@pytest.mark.parametrize(
    "content",
    [
        pytest.param(b"not-a-pid\n", id="malformed"),
        pytest.param(b"x" * (_MAX_LOCK_FILE_SIZE + 1), id="oversized"),
        pytest.param(b"not-a-pid\nhostname\n", id="two_line_bad_pid"),
        pytest.param(f"{_DEAD_PID}\nhostname\nnot-a-token\n".encode(), id="three_line_bad_start_token"),
        pytest.param(f"{_DEAD_PID}\nother-ho".encode(), id="truncated_hostname"),
    ],
)
@pytest.mark.usefixtures("unchanged_past_grace")
@NEEDS_STALE_RECLAIM
def test_unparseable_lock_evicted_when_old(lock_path: Path, content: bytes) -> None:
    lock_path.write_bytes(content)
    # An unreadable lock (bad line count, non-integer pid/start token, or oversized) must self-heal
    # instead of staying stuck; a matching line count alone does not make a file well-formed.
    _assert_self_heals(lock_path)


@pytest.mark.parametrize(
    "content",
    [
        pytest.param(b"not-a-pid\n", id="malformed"),
        pytest.param(b"", id="empty"),
        pytest.param(b"not-a-pid\nhostname\n", id="two_line_bad_pid"),
    ],
)
def test_unparseable_lock_not_evicted_when_fresh(lock_path: Path, content: bytes) -> None:
    lock_path.write_bytes(content)
    _assert_times_out(lock_path)


@pytest.mark.skipif(sys.platform == "win32", reason="st_ctime is the creation time on Windows, which no change moves")
@pytest.mark.usefixtures("unchanged_past_grace")
def test_unparseable_lock_changed_under_its_old_mtime_and_inode_not_evicted(  # pragma: win32 no cover
    lock_path: Path,
) -> None:
    # Another file can repeat both: its owner sets the mtime, and ext4 hands a freed inode number to the next file.
    lock_path.write_bytes(b"not-a-pid\n")
    watched: Final = lock_path.stat()
    contender: Final = SoftFileLock(lock_path)
    with pytest.raises(Timeout):
        contender.acquire(blocking=False)
    while lock_path.stat().st_ctime_ns == watched.st_ctime_ns:
        os.utime(lock_path, ns=(watched.st_atime_ns, watched.st_mtime_ns))
    with pytest.raises(Timeout):
        contender.acquire(blocking=False)
    assert lock_path.exists()


@_REQUIRES_START_TOKEN
def test_fresh_marker_with_truncated_start_token_not_evicted(lock_path: Path) -> None:
    # A reader racing our own write sees a cut start token, which must not pass for a recycled PID and break our lock.
    lock_path.write_text(f"{os.getpid()}\n{_HOST}\n{process_start_token(os.getpid())}"[:-1], encoding="utf-8")
    _assert_times_out(lock_path)


@pytest.mark.parametrize(
    "pid",
    [
        pytest.param(0, id="zero"),
        pytest.param(-1, id="negative"),
        pytest.param(2**31, id="oversized"),
    ],
)
@pytest.mark.usefixtures("unchanged_past_grace")
@NEEDS_STALE_RECLAIM
def test_out_of_range_pid_self_heals_when_old(lock_path: Path, pid: int) -> None:
    lock_path.write_text(_holder(pid), encoding="utf-8")
    # pid 0 or -1 makes os.kill probe the caller's own process group (reads as alive), so the lock is never
    # reclaimed; an oversized pid raises OverflowError out of stale detection. Both are malformed and must
    # self-heal, matching what _parse_marker_bytes rejects.
    _assert_self_heals(lock_path)


def test_out_of_range_pid_not_evicted_when_fresh(lock_path: Path) -> None:
    lock_path.write_text(_holder(0), encoding="utf-8")
    # A fresh out-of-range pid is malformed too, but like any malformed lock it is left alone until it ages
    # past the threshold, so a peer mid-write is not mistaken for a stale lock.
    _assert_times_out(lock_path)


@pytest.mark.usefixtures("break_route")
@pytest.mark.parametrize("break_route", ["no-exchange"], indirect=True)
def test_stale_lock_rename_race(lock_path: Path, mocker: MockerFixture) -> None:
    lock_path.write_text(_holder(_DEAD_PID), encoding="utf-8")
    mocker.patch("filelock._identity.process_alive", return_value=False)
    mocker.patch.object(Path, "rename", side_effect=FileNotFoundError("already gone"))
    _assert_times_out(lock_path)


def test_stale_break_racing_a_successor_admits_no_third_holder(
    lock_path: Path, mocker: MockerFixture, on_break_recheck: Callable[[Path, Callable[[], None]], None]
) -> None:
    lock_path.write_text(_holder(_DEAD_PID), encoding="utf-8")
    successor: Final[SoftFileLock] = SoftFileLock(lock_path, timeout=0)

    def successor_breaks_and_acquires_first() -> None:
        lock_path.unlink()
        successor.acquire()

    on_break_recheck(lock_path, successor_breaks_and_acquires_first)
    _assert_times_out(lock_path)
    mocker.stopall()

    _assert_times_out(lock_path)
    successor.release()


@pytest.mark.skipif(sys.platform == "win32", reason="Windows cannot exchange two names")
@NEEDS_STALE_RECLAIM
def test_stale_break_keeps_the_lock_when_a_peer_swaps_its_marker_aside(  # pragma: win32 no cover
    lock_path: Path, mocker: MockerFixture
) -> None:
    lock_path.write_text(_holder(_DEAD_PID), encoding="utf-8")
    (peer := lock_path.with_name("peer")).write_text(_holder(os.getpid()), encoding="utf-8")
    unlink: Final = Path.unlink

    def peer_exchanges_then_unlink(path: Path, *, missing_ok: bool = False) -> None:
        exchange_names(str(lock_path), str(peer))
        unlink(path, missing_ok=missing_ok)

    mocker.patch.object(Path, "unlink", new=peer_exchanges_then_unlink)
    lock: Final = SoftFileLock(lock_path)
    lock.acquire(blocking=False)
    mocker.stopall()
    exchange_names(str(lock_path), str(peer))

    assert (lock.is_locked, lock.is_lock_held_by_us) == (True, True)
    lock.release()
    assert list(lock_path.parent.glob("test.lock*")) == []


def _break_name(lock: Path, pid: int = _DEAD_PID) -> Path:  # pragma: win32 no cover
    return lock.with_name(f"test.lock.break.{pid}.{'0' * 32}")


def _break_contents(lock: Path) -> list[str]:  # pragma: win32 no cover
    return [path.read_text(encoding="utf-8") for path in lock.parent.glob(f"{lock.name}.break.*")]


def _live(record: str) -> str:  # pragma: win32 no cover
    return record.replace(str(_DEAD_PID), str(os.getpid()))


@pytest.mark.skipif(sys.platform == "win32", reason="Windows cannot exchange two names")
@STALE_BREAKERS
@NEEDS_STALE_RECLAIM
def test_stale_break_restores_a_live_marker_a_dead_breaker_left_under_its_break_name(  # pragma: win32 no cover
    tmp_path: Path, lock_type: type[SoftFileLock], record: str
) -> None:
    (lock := tmp_path / "test.lock").write_text(record, encoding="utf-8")
    _break_name(lock).write_text(_live(record), encoding="utf-8")
    with pytest.raises(Timeout):
        lock_type(lock).acquire(blocking=False)

    assert (lock.read_text(encoding="utf-8"), list(tmp_path.glob("test.lock.break.*"))) == (_live(record), [])


@pytest.mark.skipif(sys.platform == "win32", reason="Windows cannot exchange two names")
@STALE_BREAKERS
@NEEDS_STALE_RECLAIM
def test_stale_break_restores_a_marker_of_another_host_with_the_breakers_pid(  # pragma: win32 no cover
    tmp_path: Path, lock_type: type[SoftFileLock], record: str
) -> None:
    (lock := tmp_path / "test.lock").write_text(record, encoding="utf-8")
    foreign: Final = record.replace(_HOST, "elsewhere")
    _break_name(lock).write_text(foreign, encoding="utf-8")
    with pytest.raises(Timeout):
        lock_type(lock).acquire(blocking=False)

    assert (lock.read_text(encoding="utf-8"), list(tmp_path.glob("test.lock.break.*"))) == (foreign, [])


def _restore_name(lock: Path, pid: int | str) -> Path:  # pragma: win32 no cover
    return lock.with_name(f"test.lock.restore.{pid}.{'0' * 32}")


@pytest.mark.skipif(sys.platform == "win32", reason="Windows cannot exchange two names")
@STALE_BREAKERS
@NEEDS_STALE_RECLAIM
def test_stale_break_restores_under_an_exclusive_claim(  # pragma: win32 no cover
    tmp_path: Path, lock_type: type[SoftFileLock], record: str, mocker: MockerFixture
) -> None:
    (lock := tmp_path / "test.lock").write_text(record, encoding="utf-8")
    foreign: Final = record.replace(_HOST, "elsewhere")
    _break_name(lock).write_text(foreign, encoding="utf-8")
    first_has_checked: Final = threading.Event()
    release_rival: Final = threading.Event()
    rival_timeouts: Final[list[bool]] = []

    def rival_recovers() -> None:
        with pytest.raises(Timeout):
            lock_type(lock).acquire(blocking=False)
        rival_timeouts.append(True)

    rival: Final = threading.Thread(target=rival_recovers)
    main: Final = threading.current_thread()
    exchange: Final = exchange_names
    unlink: Final = Path.unlink

    def settle(until: threading.Event | None = None) -> None:
        while rival.is_alive() and not (until and until.is_set()):
            rival.join(0.001)

    def exchange_with_a_rival_between_check_and_exchange(first: str, second: str) -> bool:
        # The rival reaches its own exchange only when recovery lets two contenders act on one record.
        if threading.current_thread() is main:
            rival.start()
            settle(first_has_checked)
            return exchange(first, second)
        first_has_checked.set()  # pragma: no cover
        while not release_rival.is_set():  # pragma: no cover
            release_rival.wait(0.001)  # pragma: no cover
        return exchange(first, second)  # pragma: no cover

    def unlink_after_the_rival_finished(path: Path, *, missing_ok: bool = False) -> None:
        release_rival.set()
        settle()
        unlink(path, missing_ok=missing_ok)

    mocker.patch("filelock._soft.exchange_names", new=exchange_with_a_rival_between_check_and_exchange)
    mocker.patch.object(Path, "unlink", new=unlink_after_the_rival_finished)
    with pytest.raises(Timeout):
        lock_type(lock).acquire(blocking=False)
    release_rival.set()
    settle()

    assert (
        rival_timeouts,
        lock.read_text(encoding="utf-8"),
        list(tmp_path.glob("test.lock.break.*")),
        list(tmp_path.glob("test.lock.restore.*")),
    ) == ([True], foreign, [], [])


@pytest.mark.skipif(sys.platform == "win32", reason="Windows cannot exchange two names")
@STALE_BREAKERS
@NEEDS_STALE_RECLAIM
def test_stale_break_waits_when_a_peer_claimed_the_record_first(  # pragma: win32 no cover
    tmp_path: Path, lock_type: type[SoftFileLock], record: str, mocker: MockerFixture
) -> None:
    (lock := tmp_path / "test.lock").write_text(record, encoding="utf-8")
    foreign: Final = record.replace(_HOST, "elsewhere")
    _break_name(lock).write_text(foreign, encoding="utf-8")
    rename: Final = os.rename

    def peer_claims_first(source: Path, target: str) -> None:
        rename(source, _restore_name(lock, _DEAD_PID - 1))
        rename(source, target)

    mocker.patch("filelock._soft.os.rename", side_effect=peer_claims_first)
    with pytest.raises(Timeout):
        lock_type(lock).acquire(blocking=False)

    assert (lock.read_text(encoding="utf-8"), _restore_name(lock, _DEAD_PID - 1).read_text(encoding="utf-8")) == (
        record,
        foreign,
    )


@pytest.mark.skipif(sys.platform == "win32", reason="Windows cannot exchange two names")
@STALE_BREAKERS
def test_stale_break_waits_when_the_record_cannot_be_claimed(  # pragma: win32 no cover
    tmp_path: Path, lock_type: type[SoftFileLock], record: str, mocker: MockerFixture
) -> None:
    (lock := tmp_path / "test.lock").write_text(record, encoding="utf-8")
    foreign: Final = record.replace(_HOST, "elsewhere")
    _break_name(lock).write_text(foreign, encoding="utf-8")
    mocker.patch("filelock._soft.os.rename", side_effect=OSError(EACCES, "denied"))
    with pytest.raises(Timeout):
        lock_type(lock).acquire(blocking=False)

    assert (lock.read_text(encoding="utf-8"), _break_contents(lock)) == (record, [foreign])


@pytest.mark.skipif(sys.platform == "win32", reason="Windows cannot exchange two names")
@STALE_BREAKERS
@NEEDS_STALE_RECLAIM
def test_stale_break_reclaims_a_record_a_dead_recoverer_claimed(  # pragma: win32 no cover
    tmp_path: Path, lock_type: type[SoftFileLock], record: str
) -> None:
    (lock := tmp_path / "test.lock").write_text(record, encoding="utf-8")
    foreign: Final = record.replace(_HOST, "elsewhere")
    _restore_name(lock, _DEAD_PID).write_text(foreign, encoding="utf-8")
    with pytest.raises(Timeout):
        lock_type(lock).acquire(blocking=False)

    assert (lock.read_text(encoding="utf-8"), list(tmp_path.glob("test.lock.restore.*"))) == (foreign, [])


@pytest.mark.skipif(sys.platform == "win32", reason="Windows cannot exchange two names")
@STALE_BREAKERS
def test_stale_break_skips_a_record_a_live_recoverer_claimed(  # pragma: win32 no cover
    tmp_path: Path, lock_type: type[SoftFileLock], record: str
) -> None:
    (lock := tmp_path / "test.lock").write_text(record, encoding="utf-8")
    foreign: Final = record.replace(_HOST, "elsewhere")
    (claimed := _restore_name(lock, os.getppid())).write_text(foreign, encoding="utf-8")
    with pytest.raises(Timeout):
        lock_type(lock).acquire(blocking=False)

    assert (lock.read_text(encoding="utf-8"), claimed.read_text(encoding="utf-8")) == (record, foreign)


@pytest.mark.skipif(sys.platform == "win32", reason="Windows cannot exchange two names")
@STALE_BREAKERS
@NEEDS_STALE_RECLAIM
@pytest.mark.parametrize(
    "recoverer",
    [
        pytest.param(str(2**64), id="oversized"),
        pytest.param("0", id="zero"),
        # This process's pid in fullwidth digits, which int() reads back as the same number.
        pytest.param("".join(chr(0xFF10 + int(digit)) for digit in str(os.getpid())), id="wide"),
    ],
)
def test_stale_break_ignores_a_claim_naming_no_valid_pid(  # pragma: win32 no cover
    tmp_path: Path, lock_type: type[SoftFileLock], record: str, recoverer: str
) -> None:
    (lock := tmp_path / "test.lock").write_text(record, encoding="utf-8")
    foreign: Final = record.replace(_HOST, "elsewhere")
    (planted := _restore_name(lock, recoverer)).write_text(foreign, encoding="utf-8")
    with held_after_reclaim(lock_type(lock)) as held:
        assert held.is_locked

    assert planted.read_text(encoding="utf-8") == foreign


def _refuse_returning_a_claim(mocker: MockerFixture) -> None:  # pragma: win32 no cover
    rename: Final = Path.rename

    def rename_unless_returning(path: Path, target: str | Path) -> Path:
        if ".break." in str(target):
            raise OSError(EACCES, "denied")
        return rename(path, target)

    mocker.patch.object(Path, "rename", new=rename_unless_returning)


@pytest.mark.skipif(sys.platform == "win32", reason="Windows cannot exchange two names")
@STALE_BREAKERS
@NEEDS_STALE_RECLAIM
def test_stale_break_retries_a_claim_whose_return_failed(  # pragma: win32 no cover
    tmp_path: Path, lock_type: type[SoftFileLock], record: str, mocker: MockerFixture
) -> None:
    (lock := tmp_path / "test.lock").write_text(record, encoding="utf-8")
    foreign: Final = record.replace(_HOST, "elsewhere")
    _break_name(lock).write_text(foreign, encoding="utf-8")
    _refuse_returning_a_claim(mocker)
    mocker.patch("filelock._soft.exchange_names", return_value=False)
    with pytest.raises(Timeout):
        lock_type(lock).acquire(blocking=False)
    left: Final = list(tmp_path.glob(f"test.lock.restore.{os.getpid()}.*"))
    mocker.stopall()
    with pytest.raises(Timeout):
        lock_type(lock).acquire(blocking=False)

    assert (len(left), lock.read_text(encoding="utf-8"), sorted(tmp_path.iterdir())) == (1, foreign, [lock])


@pytest.mark.skipif(sys.platform == "win32", reason="Windows cannot exchange two names")
@STALE_BREAKERS
@NEEDS_STALE_RECLAIM
def test_stale_break_finishes_a_claim_an_unreadable_record_interrupted(  # pragma: win32 no cover
    tmp_path: Path, lock_type: type[SoftFileLock], record: str, mocker: MockerFixture
) -> None:
    (lock := tmp_path / "test.lock").write_text(record, encoding="utf-8")
    foreign: Final = record.replace(_HOST, "elsewhere")
    _break_name(lock).write_text(foreign, encoding="utf-8")
    read: Final = _read_lock_file

    def read_unless_claimed(path: str) -> tuple[str | None, os.stat_result]:
        if ".restore." in path:
            raise OSError(EIO, "unreadable")
        return read(path)

    _refuse_returning_a_claim(mocker)
    mocker.patch("filelock._soft._read_lock_file", new=read_unless_claimed)
    with pytest.raises(Timeout):
        lock_type(lock).acquire(blocking=False)
    left: Final = list(tmp_path.glob(f"test.lock.restore.{os.getpid()}.*"))
    mocker.stopall()
    with pytest.raises(Timeout):
        lock_type(lock).acquire(blocking=False)

    assert (len(left), lock.read_text(encoding="utf-8"), sorted(tmp_path.iterdir())) == (1, foreign, [lock])


@pytest.mark.skipif(sys.platform == "win32", reason="Windows cannot exchange two names")
@STALE_BREAKERS
@NEEDS_STALE_RECLAIM
def test_stale_break_waits_for_a_claim_another_thread_is_working(  # pragma: win32 no cover
    tmp_path: Path, lock_type: type[SoftFileLock], record: str, mocker: MockerFixture
) -> None:
    (lock := tmp_path / "test.lock").write_text(record, encoding="utf-8")
    _break_name(lock).write_text(record.replace(_HOST, "elsewhere"), encoding="utf-8")
    working, release = threading.Event(), threading.Event()
    exchange: Final = exchange_names

    def exchange_when_released(first: str, second: str) -> bool:
        working.set()
        release.wait(5)
        return exchange(first, second)

    mocker.patch("filelock._soft.exchange_names", new=exchange_when_released)
    worker: Final = threading.Thread(target=_acquire, args=(lock_type, lock))
    worker.start()
    assert working.wait(5)
    claims_while_working: Final = list(tmp_path.glob(f"test.lock.restore.{os.getpid()}.*"))
    with pytest.raises(Timeout):
        lock_type(lock).acquire(blocking=False)
    untouched: Final = list(tmp_path.glob(f"test.lock.restore.{os.getpid()}.*"))
    release.set()
    worker.join()

    assert (len(claims_while_working), untouched == claims_while_working) == (1, True)


def _acquire_timing_out(  # pragma: win32 no cover
    lock_type: type[SoftFileLock], lock: Path, outcomes: list[bool]
) -> None:
    with pytest.raises(Timeout):
        lock_type(lock).acquire(blocking=False)
    outcomes.append(True)


def _acquire(lock_type: type[SoftFileLock], lock: Path) -> None:  # pragma: win32 no cover
    with suppress(Timeout):
        lock_type(lock).acquire(blocking=False)


@pytest.mark.skipif(sys.platform == "win32", reason="Windows cannot exchange two names")
@STALE_BREAKERS
@NEEDS_STALE_RECLAIM
def test_stale_break_claim_is_registered_before_it_is_visible(  # pragma: win32 no cover
    tmp_path: Path, lock_type: type[SoftFileLock], record: str, mocker: MockerFixture
) -> None:
    (lock := tmp_path / "test.lock").write_text(record, encoding="utf-8")
    foreign: Final = record.replace(_HOST, "elsewhere")
    _break_name(lock).write_text(foreign, encoding="utf-8")
    rename: Final = Path.rename
    rival_timeouts: Final[list[bool]] = []
    claims_after_rival: Final[list[list[Path]]] = []

    def rival_scans_after_the_claim_rename(path: Path, target: str | Path) -> Path:
        renamed: Final = rename(path, target)
        mocker.stopall()
        rival: Final = threading.Thread(target=_acquire_timing_out, args=(lock_type, lock, rival_timeouts))
        rival.start()
        rival.join()
        claims_after_rival.append(list(tmp_path.glob("test.lock.restore.*")))
        return renamed

    mocker.patch.object(Path, "rename", new=rival_scans_after_the_claim_rename)
    with pytest.raises(Timeout):
        lock_type(lock).acquire(blocking=False)

    assert (
        rival_timeouts,
        len(claims_after_rival[0]),
        lock.read_text(encoding="utf-8"),
        sorted(tmp_path.iterdir()),
    ) == (
        [True],
        1,
        foreign,
        [lock],
    )


@pytest.mark.skipif(sys.platform == "win32", reason="Windows cannot exchange two names")
@STALE_BREAKERS
def test_stale_break_stops_when_the_claim_is_already_registered(  # pragma: win32 no cover
    tmp_path: Path, lock_type: type[SoftFileLock], record: str, mocker: MockerFixture
) -> None:
    (lock := tmp_path / "test.lock").write_text(record, encoding="utf-8")
    foreign: Final = record.replace(_HOST, "elsewhere")
    _break_name(lock).write_text(foreign, encoding="utf-8")
    mocker.patch("filelock._soft.secrets.token_hex", return_value="a" * 32)
    registered: Final = str(lock) + f".restore.{os.getpid()}." + "a" * 32
    assert _begin_claim(registered)
    try:
        with pytest.raises(Timeout):
            lock_type(lock).acquire(blocking=False)
    finally:
        _end_claim(registered)

    assert (lock.read_text(encoding="utf-8"), _break_contents(lock)) == (record, [foreign])


@pytest.mark.skipif(sys.platform == "win32", reason="Windows cannot exchange two names")
@STALE_BREAKERS
def test_stale_break_claim_identity_ignores_path_spelling(  # pragma: win32 no cover
    tmp_path: Path, lock_type: type[SoftFileLock], record: str, mocker: MockerFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    (lock := tmp_path / "test.lock").write_text(record, encoding="utf-8")
    foreign: Final = record.replace(_HOST, "elsewhere")
    _break_name(lock).write_text(foreign, encoding="utf-8")
    mocker.patch("filelock._soft.secrets.token_hex", return_value="a" * 32)
    registered: Final = Path(f"{lock}.restore.{os.getpid()}.{'a' * 32}")
    assert _begin_claim(registered)
    monkeypatch.chdir(tmp_path)
    try:
        with pytest.raises(Timeout):
            lock_type("./test.lock").acquire(blocking=False)
    finally:
        _end_claim(registered)

    assert (lock.read_text(encoding="utf-8"), _break_contents(lock)) == (record, [foreign])


@pytest.mark.skipif(sys.platform == "win32", reason="Windows cannot exchange two names")
@pytest.mark.parametrize("interrupted", ["rename", "read", "exchange"])
@STALE_BREAKERS
@NEEDS_STALE_RECLAIM
def test_stale_break_releases_the_registration_when_recovery_is_interrupted(  # pragma: win32 no cover
    tmp_path: Path, lock_type: type[SoftFileLock], record: str, interrupted: str, mocker: MockerFixture
) -> None:
    (lock := tmp_path / "test.lock").write_text(record, encoding="utf-8")
    foreign: Final = record.replace(_HOST, "elsewhere")
    _break_name(lock).write_text(foreign, encoding="utf-8")
    rename: Final = Path.rename

    def rename_then_interrupt(path: Path, target: str | Path) -> Path:
        rename(path, target)
        raise KeyboardInterrupt

    def read_the_claim_then_interrupt(path: str) -> tuple[str | None, os.stat_result]:
        if ".restore." in path:
            raise KeyboardInterrupt
        return _read_lock_file(path)

    def exchange_then_interrupt(*_names: str) -> bool:
        raise KeyboardInterrupt

    target, interruption = {
        "rename": ("pathlib.Path.rename", rename_then_interrupt),
        "read": ("filelock._soft._read_lock_file", read_the_claim_then_interrupt),
        "exchange": ("filelock._soft.exchange_names", exchange_then_interrupt),
    }[interrupted]
    mocker.patch(target, new=interruption)
    with pytest.raises(KeyboardInterrupt):
        lock_type(lock).acquire(blocking=False)
    mocker.stopall()
    (claim,) = tmp_path.glob("test.lock.restore.*")
    released: Final = _begin_claim(claim)
    _end_claim(claim)
    with pytest.raises(Timeout):
        lock_type(lock).acquire(blocking=False)

    assert (released, lock.read_text(encoding="utf-8"), sorted(tmp_path.iterdir())) == (True, foreign, [lock])


@pytest.mark.skipif(sys.platform == "win32", reason="Windows cannot exchange two names")
@STALE_BREAKERS
@NEEDS_STALE_RECLAIM
def test_stale_break_undoes_an_exchange_that_took_a_peers_marker(  # pragma: win32 no cover
    tmp_path: Path, lock_type: type[SoftFileLock], record: str, mocker: MockerFixture
) -> None:
    (lock := tmp_path / "test.lock").write_text(record, encoding="utf-8")
    foreign: Final = record.replace(_HOST, "elsewhere")
    _break_name(lock).write_text(foreign, encoding="utf-8")
    (peer := tmp_path / "peer").write_text(_live(record), encoding="utf-8")
    exchange: Final = exchange_names

    def peer_takes_the_path_then_exchange(first: str, second: str) -> bool:
        mocker.patch("filelock._soft.exchange_names", new=exchange)
        peer.replace(lock)
        return exchange(first, second)

    mocker.patch("filelock._soft.exchange_names", new=peer_takes_the_path_then_exchange)
    with pytest.raises(Timeout):
        lock_type(lock).acquire(blocking=False)

    assert (
        lock.read_text(encoding="utf-8"),
        _break_contents(lock),
        list(tmp_path.glob("test.lock.restore.*")),
    ) == (_live(record), [foreign], [])


def _lose_to_a_live_holder(  # pragma: win32 no cover
    lock: Path, live: str, mocker: MockerFixture, then: Callable[[str, str], bool | None]
) -> None:
    (holder := lock.with_name("holder")).write_text(live, encoding="utf-8")

    def exchange_after_the_holder_won(aside: str, path: str) -> bool:
        mocker.patch("filelock._soft.exchange_names", new=exchange_names)
        holder.replace(lock)
        exchange_names(aside, path)
        then(aside, path)
        return True

    mocker.patch("filelock._soft.exchange_names", new=exchange_after_the_holder_won)


@pytest.mark.skipif(sys.platform == "win32", reason="Windows cannot exchange two names")
@STALE_BREAKERS
@NEEDS_STALE_RECLAIM
def test_stale_break_exchanges_back_after_the_breaker_that_took_its_marker(  # pragma: win32 no cover
    tmp_path: Path, lock_type: type[SoftFileLock], record: str, mocker: MockerFixture
) -> None:
    (lock := tmp_path / "test.lock").write_text(record, encoding="utf-8")
    (peer := tmp_path / "peer").write_text(record.replace(_HOST, "elsewhere"), encoding="utf-8")

    def peer_exchanges_back(_seconds: float) -> None:
        exchange_names(str(peer), str(lock))
        peer.unlink()

    _lose_to_a_live_holder(lock, _live(record), mocker, lambda _aside, path: exchange_names(str(peer), path))
    mocker.patch("filelock._util.time", wraps=time, sleep=mocker.Mock(side_effect=peer_exchanges_back))
    with pytest.raises(Timeout):
        lock_type(lock).acquire(blocking=False)

    assert (lock.read_text(encoding="utf-8"), sorted(tmp_path.iterdir())) == (_live(record), [lock])


@pytest.mark.skipif(sys.platform == "win32", reason="Windows cannot exchange two names")
@STALE_BREAKERS
@NEEDS_STALE_RECLAIM
def test_stale_break_opens_a_ring_of_breakers_holding_each_others_markers(  # pragma: win32 no cover
    tmp_path: Path, lock_type: type[SoftFileLock], record: str, mocker: MockerFixture
) -> None:
    (lock := tmp_path / "test.lock").write_text(record, encoding="utf-8")
    foreign: Final = record.replace(_HOST, "elsewhere")
    (peer := tmp_path / "peer").write_text(foreign, encoding="utf-8")

    def peer_then_our_exchange_out_of_turn(aside: str, path: str) -> None:
        exchange_names(str(peer), path)
        exchange_names(aside, path)

    def peer_exchanges_back_once_it_stands_at_the_path(_seconds: float) -> None:
        if lock.read_text(encoding="utf-8") == foreign:
            exchange_names(str(peer), str(lock))
            peer.unlink()

    _lose_to_a_live_holder(lock, _live(record), mocker, peer_then_our_exchange_out_of_turn)
    mocker.patch(
        "filelock._util.time", wraps=time, sleep=mocker.Mock(side_effect=peer_exchanges_back_once_it_stands_at_the_path)
    )
    with pytest.raises(Timeout):
        lock_type(lock).acquire(blocking=False)

    assert (lock.read_text(encoding="utf-8"), sorted(tmp_path.iterdir())) == (_live(record), [lock])


@pytest.mark.skipif(sys.platform == "win32", reason="Windows cannot exchange two names")
@STALE_BREAKERS
@NEEDS_STALE_RECLAIM
def test_stale_break_keeps_every_name_when_the_path_never_comes_back(  # pragma: win32 no cover
    tmp_path: Path, lock_type: type[SoftFileLock], record: str, mocker: MockerFixture
) -> None:
    (lock := tmp_path / "test.lock").write_text(record, encoding="utf-8")
    foreign: Final = record.replace(_HOST, "elsewhere")
    (peer := tmp_path / "peer").write_text(foreign, encoding="utf-8")
    _lose_to_a_live_holder(lock, _live(record), mocker, lambda _aside, path: exchange_names(str(peer), path))
    with pytest.raises(Timeout):
        lock_type(lock).acquire(blocking=False)

    assert sorted([lock.read_text(encoding="utf-8"), *_break_contents(lock)]) == sorted([foreign, _live(record)])


@pytest.mark.skipif(sys.platform == "win32", reason="Windows cannot exchange two names")
@STALE_BREAKERS
@NEEDS_STALE_RECLAIM
def test_stale_break_keeps_its_marker_when_exchange_back_is_refused(  # pragma: win32 no cover
    tmp_path: Path, lock_type: type[SoftFileLock], record: str, mocker: MockerFixture
) -> None:
    (lock := tmp_path / "test.lock").write_text(record, encoding="utf-8")
    _lose_to_a_live_holder(lock, _live(record), mocker, lambda _aside, _path: None)
    mocker.patch("filelock._util.exchange_names", side_effect=[False, AssertionError("retried refused exchange")])
    with pytest.raises(Timeout):
        lock_type(lock).acquire(blocking=False)

    assert (f"{os.getpid()}\n" in lock.read_text(encoding="utf-8"), _break_contents(lock)) == (
        True,
        [_live(record)],
    )


@pytest.mark.skipif(sys.platform == "win32", reason="Windows cannot exchange two names")
@STALE_BREAKERS
@NEEDS_STALE_RECLAIM
def test_stale_break_drops_what_it_took_when_the_holder_released_the_path(  # pragma: win32 no cover
    tmp_path: Path, lock_type: type[SoftFileLock], record: str, mocker: MockerFixture
) -> None:
    (lock := tmp_path / "test.lock").write_text(record, encoding="utf-8")
    _lose_to_a_live_holder(lock, _live(record), mocker, lambda _aside, path: Path(path).unlink())
    with pytest.raises(Timeout):
        lock_type(lock).acquire(blocking=False)

    assert list(tmp_path.iterdir()) == []


@pytest.mark.skipif(sys.platform == "win32", reason="Windows cannot exchange two names")
@STALE_BREAKERS
@NEEDS_STALE_RECLAIM
def test_stale_break_leaves_a_marker_taken_since_its_first_look_on_the_path(  # pragma: win32 no cover
    tmp_path: Path,
    lock_type: type[SoftFileLock],
    record: str,
    mocker: MockerFixture,
    on_break_recheck: Callable[[Path, Callable[[], None]], None],
) -> None:
    (lock := tmp_path / "test.lock").write_text(record, encoding="utf-8")
    (holder := tmp_path / "holder").write_text(_live(record), encoding="utf-8")
    exchange: Final = mocker.spy(filelock._soft, "exchange_names")

    def peer_takes_the_marker() -> None:
        holder.replace(lock)

    on_break_recheck(lock, peer_takes_the_marker)
    with pytest.raises(Timeout):
        lock_type(lock).acquire(blocking=False)

    assert (exchange.call_count, lock.read_text(encoding="utf-8"), sorted(tmp_path.iterdir())) == (
        0,
        _live(record),
        [lock],
    )


@pytest.mark.skipif(sys.platform == "win32", reason="Windows cannot exchange two names")
@STALE_BREAKERS
@NEEDS_STALE_RECLAIM
def test_stale_break_removes_a_stale_marker_a_dead_breaker_left_under_its_break_name(  # pragma: win32 no cover
    tmp_path: Path, lock_type: type[SoftFileLock], record: str
) -> None:
    (lock := tmp_path / "test.lock").write_text(record, encoding="utf-8")
    _break_name(lock).write_text(record.replace(str(_DEAD_PID), str(_DEAD_PID - 1)), encoding="utf-8")
    contender: Final = lock_type(lock)
    contender.acquire(blocking=False)

    assert (contender.is_locked, list(tmp_path.glob("test.lock.break.*"))) == (True, [])
    contender.release()


@pytest.mark.skipif(sys.platform == "win32", reason="Windows cannot exchange two names")
@STALE_BREAKERS
@NEEDS_STALE_RECLAIM
def test_stale_break_restores_a_strict_sentinel_a_dead_breaker_left_under_its_break_name(  # pragma: win32 no cover
    tmp_path: Path, lock_type: type[SoftFileLock], record: str
) -> None:
    (lock := tmp_path / "test.lock").write_text(record, encoding="utf-8")
    _break_name(lock).write_text(STRICT_SOFT_SENTINEL_RECORD, encoding="utf-8")
    with pytest.raises(Timeout):
        lock_type(lock).acquire(blocking=False)

    assert lock.read_text(encoding="utf-8") == STRICT_SOFT_SENTINEL_RECORD


@pytest.mark.skipif(sys.platform == "win32", reason="Windows cannot exchange two names")
@pytest.mark.parametrize("lock_type", [MarkerSoftFileLock, SoftFileLease])
@NEEDS_STALE_RECLAIM
def test_stale_break_restores_a_marker_of_an_unknown_contract_under_its_break_name(  # pragma: win32 no cover
    tmp_path: Path, lock_type: type[MarkerSoftFileLock]
) -> None:
    (lock := tmp_path / "test.lock").write_text(DEAD_OWNER, encoding="utf-8")
    future: Final = DEAD_OWNER.replace(str(_DEAD_PID), str(_DEAD_PID - 1)).replace("exclusive", "future")
    _break_name(lock).write_text(future, encoding="utf-8")
    with pytest.raises(Timeout):
        lock_type(lock).acquire(blocking=False)

    assert lock.read_text(encoding="utf-8") == future


@pytest.mark.skipif(sys.platform == "win32", reason="Windows cannot exchange two names")
@pytest.mark.usefixtures("unchanged_past_grace")
@STALE_BREAKERS
@NEEDS_STALE_RECLAIM
def test_stale_break_restores_a_marker_a_dead_breaker_left_beside_its_placeholder(  # pragma: win32 no cover
    tmp_path: Path, lock_type: type[SoftFileLock], record: str
) -> None:
    (lock := tmp_path / "test.lock").write_text(f"filelock/break\n{_DEAD_PID}\n", encoding="utf-8")
    _break_name(lock).write_text(_live(record), encoding="utf-8")
    contender: Final = lock_type(lock)
    for _ in range(2):
        with suppress(Timeout):
            contender.acquire(blocking=False)

    assert (contender.is_locked, lock.read_text(encoding="utf-8"), list(tmp_path.glob("test.lock.break.*"))) == (
        False,
        _live(record),
        [],
    )


@pytest.mark.skipif(sys.platform == "win32", reason="Windows cannot exchange two names")
@pytest.mark.usefixtures("unchanged_past_grace")
@pytest.mark.parametrize(
    "placeholder",
    [
        pytest.param("filelock/break\n", id="no-pid"),
        pytest.param("filelock/break\nnot-a-pid\n", id="not-a-pid"),
        pytest.param(f"filelock/break\n{_DEAD_PID}", id="torn-pid"),
    ],
)
@NEEDS_STALE_RECLAIM
def test_stale_break_evicts_a_placeholder_that_names_no_breaker(  # pragma: win32 no cover
    tmp_path: Path, placeholder: str
) -> None:
    (lock := tmp_path / "test.lock").write_text(placeholder, encoding="utf-8")
    _break_name(lock).write_text(_holder(os.getpid()), encoding="utf-8")
    contender: Final = SoftFileLock(lock)
    for _ in range(2):
        with suppress(Timeout):
            contender.acquire(blocking=False)

    assert (contender.is_locked, _break_name(lock).exists()) == (True, True)
    contender.release()


@pytest.mark.skipif(sys.platform == "win32", reason="Windows cannot exchange two names")
@pytest.mark.parametrize("error", [pytest.param(EACCES, id="denied"), pytest.param(EINVAL, id="unsupported")])
def test_stale_break_leaves_a_break_name_it_cannot_exchange(  # pragma: win32 no cover
    lock_path: Path, mocker: MockerFixture, error: int
) -> None:
    lock_path.write_text(_holder(_DEAD_PID), encoding="utf-8")
    _break_name(lock_path).write_text(_holder(os.getpid()), encoding="utf-8")
    mocker.patch("filelock._util._rename_call", return_value=(mocker.MagicMock(return_value=error), 2))
    with pytest.raises(Timeout):
        SoftFileLock(lock_path).acquire(blocking=False)

    assert (lock_path.read_text(encoding="utf-8"), _break_contents(lock_path)) == (
        _holder(_DEAD_PID),
        [_holder(os.getpid())],
    )


@pytest.mark.skipif(sys.platform == "win32", reason="Windows cannot exchange two names")
def test_stale_break_waits_when_a_break_name_cannot_be_read(  # pragma: win32 no cover
    lock_path: Path, mocker: MockerFixture
) -> None:
    lock_path.write_text(_holder(_DEAD_PID), encoding="utf-8")
    _break_name(lock_path).write_text(_holder(os.getpid()), encoding="utf-8")
    open_: Final = os.open

    def refuse_the_break_name(path: str, flags: int, mode: int = 0o777, *, dir_fd: int | None = None) -> int:
        if ".restore." in os.fspath(path):
            raise OSError(EACCES, "Permission denied", path)
        return open_(path, flags, mode, dir_fd=dir_fd)

    mocker.patch.object(os, "open", new=refuse_the_break_name)
    with pytest.raises(Timeout):
        SoftFileLock(lock_path).acquire(blocking=False)
    mocker.stopall()

    assert (lock_path.read_text(encoding="utf-8"), _break_contents(lock_path)) == (
        _holder(_DEAD_PID),
        [_holder(os.getpid())],
    )


@pytest.mark.skipif(sys.platform == "win32", reason="Windows cannot exchange two names")
@pytest.mark.timeout(_PROCESS_DEADLINE * 2)
@NEEDS_STALE_RECLAIM
def test_stale_break_after_a_breaker_was_killed_keeps_the_holder_alone(  # pragma: win32 no cover
    lock_path: Path,
) -> None:
    lock_path.write_text(_holder(_DEAD_PID), encoding="utf-8")
    context: Final = get_context("spawn")
    ready, go, exchanged = context.Event(), context.Event(), context.Event()
    breaker: Final = context.Process(target=break_then_hang_after_exchange, args=(str(lock_path), ready, go, exchanged))
    holder: Final = SoftFileLock(lock_path)
    with cleanup_processes([breaker]):
        breaker.start()
        assert ready.wait(_PROCESS_DEADLINE)
        holder.acquire(blocking=False)
        go.set()
        assert exchanged.wait(_PROCESS_DEADLINE)
        breaker.terminate()
        breaker.join(_PROCESS_DEADLINE)
        with pytest.raises(Timeout):
            SoftFileLock(lock_path).acquire(blocking=False)

        assert (holder.is_locked, holder.is_lock_held_by_us) == (True, True)
    holder.release()


@NEEDS_SYMLINK
def test_symlinked_lock_file_is_not_followed(tmp_path: Path, lock_path: Path) -> None:  # pragma: needs symlink
    target = tmp_path / "target"
    target.write_text(_holder(99999), encoding="utf-8")
    lock_path.symlink_to(target)

    # Neither the pid read nor stale detection may follow the symlink onto the target file.
    assert SoftFileLock(lock_path).pid is None
    assert target.read_text(encoding="utf-8") == _holder(99999)


def test_fifo_lock_file_does_not_block(lock_path: Path) -> None:
    if sys.platform == "win32" or not CAPABILITIES["fifo"]:  # pragma: win32 cover
        pytest.skip("os.mkfifo is unavailable")  # the platform arm also narrows so ty resolves os.mkfifo below
    # An attacker-placed FIFO must not stall the open; O_NONBLOCK makes the read bail instead of hang.
    os.mkfifo(lock_path)  # pragma: win32 no cover
    assert SoftFileLock(lock_path).pid is None  # pragma: win32 no cover


@pytest.mark.usefixtures("unchanged_past_grace")
@NEEDS_STALE_RECLAIM
def test_fifo_lock_file_with_attached_writer_self_heals(lock_path: Path) -> None:
    if sys.platform == "win32" or not CAPABILITIES["fifo"]:  # pragma: win32 cover
        pytest.skip("os.mkfifo is unavailable")  # the platform arm also narrows so ty resolves os.mkfifo below
    # A same-UID peer can plant a FIFO with a writer attached so a non-blocking read would raise EAGAIN. The lstat
    # guard classifies it as a malformed lock before any open, so an aged FIFO self-heals like any other node.
    os.mkfifo(lock_path)  # pragma: win32 no cover
    reader = os.open(lock_path, os.O_RDONLY | os.O_NONBLOCK)  # pragma: win32 no cover
    writer = os.open(lock_path, os.O_WRONLY | os.O_NONBLOCK)  # pragma: win32 no cover
    try:  # pragma: win32 no cover
        _assert_self_heals(lock_path)
    finally:
        os.close(reader)  # pragma: win32 no cover
        os.close(writer)  # pragma: win32 no cover


@pytest.mark.usefixtures("unchanged_past_grace")
@NEEDS_STALE_RECLAIM
def test_socket_lock_file_self_heals(lock_path: Path) -> None:
    if sys.platform == "win32":  # pragma: win32 cover
        pytest.skip("AF_UNIX sockets are unix-only")
    sock = socket.socket(socket.AF_UNIX)  # pragma: win32 no cover
    try:  # pragma: win32 no cover
        sock.bind(str(lock_path))
    except OSError:  # pragma: darwin cover
        sock.close()  # pragma: darwin cover
        pytest.skip("AF_UNIX path too long for this temp dir")  # pragma: darwin cover
    # A Unix-domain socket cannot be os.open()ed as a file. Before the lstat guard the failed open was swallowed by
    # stale detection so acquisition wedged; an aged socket now self-heals like any other non-regular node.
    try:  # pragma: linux cover
        _assert_self_heals(lock_path)
    finally:
        sock.close()  # pragma: linux cover


def test_stale_detection_errors_suppressed(lock_path: Path, mocker: MockerFixture) -> None:
    lock_path.write_text(_holder(os.getpid()), encoding="utf-8")
    mock_read: MagicMock = mocker.patch("filelock._soft._read_lock_file", side_effect=OSError("read failed"))
    _assert_times_out(lock_path)
    mock_read.assert_called()


@_WINDOWS_ONLY
@pytest.mark.parametrize(
    ("captured_error", "ambient_error", "acquires"),
    [
        pytest.param(_WIN_ERROR_INVALID_PARAMETER, _WIN_ERROR_ACCESS_DENIED, True, id="dead"),
        pytest.param(_WIN_ERROR_ACCESS_DENIED, _WIN_ERROR_INVALID_PARAMETER, False, id="access-denied"),
    ],
)
def test_windows_stale_lock_uses_captured_process_error(  # pragma: win32 cover
    lock_path: Path,
    mocker: MockerFixture,
    captured_error: int,
    ambient_error: int,
    *,
    acquires: bool,
) -> None:
    if sys.platform != "win32":  # pragma: win32 cover
        pytest.skip("windows-only")  # pragma: no cover  # win32-only test; this guard never runs
    import ctypes

    def fail_open_process(_access: int, _inherit_handle: bool, _pid: int) -> None:
        ctypes.set_last_error(captured_error)
        ctypes.windll.kernel32.SetLastError(ambient_error)

    # ctypes function pointers do not expose a Python signature for autospec.
    mocker.patch("filelock._identity._KERNEL32.OpenProcess", side_effect=fail_open_process)
    lock_path.write_text(_holder(_DEAD_PID), encoding="utf-8")
    if acquires:
        _assert_self_heals(lock_path)
    else:
        _assert_times_out(lock_path)


@_WINDOWS_ONLY
def test_windows_stale_lock_broken_when_pid_recycled(lock_path: Path) -> None:  # pragma: win32 cover
    process_marker = lock_path.with_suffix(".process")
    with SoftFileLock(process_marker):
        start = int(process_marker.read_text(encoding="utf-8").splitlines()[2])
    lock_path.write_text(_holder(os.getpid(), start=start + 1), encoding="utf-8")
    _assert_self_heals(lock_path)


@_WINDOWS_ONLY
def test_windows_live_process_marker_retained(lock_path: Path) -> None:  # pragma: win32 cover
    lock = SoftFileLock(lock_path)
    lock.acquire()
    try:
        _assert_times_out(lock_path)
    finally:
        lock.release()


@_WINDOWS_ONLY
def test_windows_live_process_marker_without_start_token_retained(lock_path: Path) -> None:  # pragma: win32 cover
    lock_path.write_text(_holder(os.getpid()), encoding="utf-8")
    _assert_times_out(lock_path)


@_WINDOWS_ONLY
def test_windows_process_probe_closes_handles(lock_path: Path) -> None:  # pragma: win32 cover
    lock = SoftFileLock(lock_path)
    lock.acquire()
    try:
        handle_count = _current_process_handle_count()
        for _attempt in range(50):
            _assert_times_out(lock_path, timeout=0)
        # A per-probe handle leak would add one handle per iteration, so ~50 over the loop. Assert no growth beyond a
        # small margin rather than exact equality, which another thread opening or closing a handle would break.
        assert _current_process_handle_count() <= handle_count + 5
    finally:
        lock.release()


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        pytest.param(None, None, id="no_file"),
        pytest.param(b"not-a-number\n", None, id="malformed"),
        pytest.param(b"\xff\xfe\n", None, id="non_utf8"),
        pytest.param(b"x" * (_MAX_LOCK_FILE_SIZE + 1), None, id="oversized"),
        pytest.param(b"42\n", None, id="single_line"),
        pytest.param(f"{os.getpid()}\n{_HOST}".encode(), None, id="truncated"),
        pytest.param(_holder(0).encode(), None, id="out_of_range_pid"),
        pytest.param(_holder(os.getpid()).encode(), os.getpid(), id="valid"),
    ],
)
def test_pid(lock_path: Path, content: bytes | None, expected: int | None) -> None:
    if content is not None:
        lock_path.write_bytes(content)
    assert SoftFileLock(lock_path).pid == expected


def test_pid_while_locked(lock_path: Path) -> None:
    lock = SoftFileLock(lock_path)
    with lock:
        assert lock.pid == os.getpid()


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        pytest.param(None, False, id="no_file"),
        pytest.param(_holder(os.getpid() + 1), False, id="different_pid"),
        pytest.param(_holder(os.getpid()), True, id="same_pid"),
        pytest.param(_holder(os.getpid(), host="other-host"), False, id="same_pid_different_host"),
    ],
)
def test_is_lock_held_by_us(lock_path: Path, content: str | None, expected: bool) -> None:
    if content is not None:
        lock_path.write_text(content, encoding="utf-8")
    assert SoftFileLock(lock_path).is_lock_held_by_us is expected


@_REQUIRES_START_TOKEN
@pytest.mark.parametrize(
    ("offset", "expected"),
    [pytest.param(0, True, id="matching_start_token"), pytest.param(1, False, id="recycled_pid")],
)
def test_is_lock_held_by_us_checks_start_token(lock_path: Path, offset: int, *, expected: bool) -> None:
    # Acquisition breaks a marker with our PID but another start token as stale, so it must not read as ours either.
    token: Final = process_start_token(os.getpid())
    assert token is not None
    lock_path.write_text(_holder(os.getpid(), start=token + offset), encoding="utf-8")
    assert SoftFileLock(lock_path).is_lock_held_by_us is expected


def test_is_lock_held_by_us_fails_closed_without_own_start_token(lock_path: Path, mocker: MockerFixture) -> None:
    lock_path.write_text(_holder(os.getpid(), start=1), encoding="utf-8")
    mocker.patch("filelock._identity.process_start_token", return_value=None)
    assert SoftFileLock(lock_path).is_lock_held_by_us is False


@pytest.mark.parametrize(
    "exists",
    [pytest.param(True, id="exists"), pytest.param(False, id="missing")],
)
def test_break_lock(lock_path: Path, *, exists: bool) -> None:
    if exists:
        lock_path.write_text(_holder(os.getpid()), encoding="utf-8")
    SoftFileLock(lock_path).break_lock()
    assert not lock_path.exists()


def test_write_failure_rolls_back_acquire(lock_path: Path, mocker: MockerFixture) -> None:
    mocker.patch("filelock._util.os.write", side_effect=OSError(ENOSPC, "No space left on device"))

    lock = SoftFileLock(lock_path)
    with pytest.raises(OSError, match="No space left on device"):
        lock.acquire()
    assert not lock.is_locked
    assert not lock_path.exists()


def _assert_self_heals(lock_path: Path) -> None:
    with held_after_reclaim(SoftFileLock(lock_path)) as lock:
        assert lock.is_locked


def _assert_times_out(lock_path: Path, *, timeout: float = 0.1) -> None:
    with pytest.raises(TimeoutError):
        SoftFileLock(lock_path, timeout=timeout).acquire()


def _current_process_handle_count() -> int:  # pragma: win32 cover
    if sys.platform != "win32":  # pragma: win32 cover
        pytest.skip("windows-only")  # pragma: no cover  # only ever called on win32, so this guard never runs
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetCurrentProcess.argtypes = []
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.GetProcessHandleCount.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel32.GetProcessHandleCount.restype = wintypes.BOOL
    count = wintypes.DWORD()
    assert kernel32.GetProcessHandleCount(kernel32.GetCurrentProcess(), ctypes.byref(count)), ctypes.WinError(
        ctypes.get_last_error()
    )
    return count.value


def _hold_reports_foreign_identity(mocker: MockerFixture) -> None:
    # Make the held descriptor report a different inode than the file on disk, as if a peer replaced the marker at the
    # path. fstat runs only on our own lock fd, so nothing else in acquire or release is disturbed.
    mocker.patch("filelock._soft.os.fstat", return_value=mocker.Mock(st_dev=1, st_ino=999))


def test_short_writes_still_write_the_whole_record(lock_path: Path, mocker: MockerFixture) -> None:
    real_write = os.write
    mocker.patch("filelock._util.os.write", side_effect=lambda fd, data: real_write(fd, bytes(data[:1])))

    with SoftFileLock(lock_path):
        lines = lock_path.read_text(encoding="utf-8").splitlines()
    assert lines[0] == str(os.getpid())
    assert lines[1] == _HOST


def test_zero_write_rolls_back_acquire(lock_path: Path, mocker: MockerFixture) -> None:
    mocker.patch("filelock._util.os.write", return_value=0)

    lock = SoftFileLock(lock_path)
    with pytest.raises(OSError, match="0 bytes"):
        lock.acquire()
    assert not lock.is_locked
    assert not lock_path.exists()


@pytest.mark.skipif(sys.platform == "win32", reason="Windows unlinks a marker only after closing it")
def test_failed_acquire_spares_a_successor_on_the_freed_inode_number(  # pragma: win32 no cover
    lock_path: Path, mocker: MockerFixture, reuse_inode: Callable[[Path, os.stat_result], None]
) -> None:
    def evicted_and_succeeded_then_write_fails(fd: int, data: bytes) -> int:
        # A contender evicted the still-empty marker, and the next holder's marker gets its inode number once freed.
        ours: Final = os.fstat(fd)
        lock_path.unlink()
        lock_path.write_text("live", encoding="utf-8")
        os.utime(lock_path, ns=(ours.st_atime_ns, ours.st_mtime_ns))
        reuse_inode(lock_path, ours)
        raise OSError(ENOSPC, f"No space left on device for {len(data)} bytes")

    mocker.patch("filelock._util.os.write", side_effect=evicted_and_succeeded_then_write_fails)
    with pytest.raises(OSError, match="No space left on device"):
        SoftFileLock(lock_path).acquire()
    mocker.stopall()

    assert lock_path.read_text(encoding="utf-8") == "live"


@pytest.mark.skipif(sys.platform == "win32", reason="Windows cannot exchange two names")
@NEEDS_STALE_RECLAIM
def test_failed_acquire_spares_a_file_that_replaced_its_placeholder(  # pragma: win32 no cover
    lock_path: Path, mocker: MockerFixture
) -> None:
    mocker.patch("filelock._soft.os.fchmod", side_effect=OSError(EIO, "Input/output error"))
    lstat: Final = os.lstat
    replaced: list[bool] = []

    def peer_replaces_the_placeholder(path: str | os.PathLike[str], *, dir_fd: int | None = None) -> os.stat_result:
        found: Final = lstat(path, dir_fd=dir_fd)
        if ".break." in os.fspath(path) and not replaced:
            replaced.append(True)
            (other := lock_path.with_name("other")).write_text("other", encoding="utf-8")
            other.replace(lock_path)
        return found

    mocker.patch.object(os, "lstat", new=peer_replaces_the_placeholder)
    with pytest.raises(OSError, match="Input/output error"):
        SoftFileLock(lock_path, mode=0o600).acquire()
    mocker.stopall()

    assert (lock_path.read_text(encoding="utf-8"), list(lock_path.parent.glob("test.lock.break.*"))) == ("other", [])


def test_failed_acquire_cleanup_spares_a_replacement(lock_path: Path, mocker: MockerFixture) -> None:
    mocker.patch("filelock._util.os.write", side_effect=OSError(ENOSPC, "No space left on device"))
    _hold_reports_foreign_identity(mocker)

    lock = SoftFileLock(lock_path)
    with pytest.raises(OSError, match="No space left on device"):
        lock.acquire()
    assert lock_path.exists()


def test_publication_without_identity_leaves_the_marker(lock_path: Path, mocker: MockerFixture) -> None:
    # The rollback cannot prove the path still names the file it created, so it leaves the marker to stale handling.
    open_spy: Final = mocker.spy(os, "open")
    real_fstat: Final = os.fstat

    def no_identity(fd: int) -> os.stat_result:
        if fd == open_spy.spy_return:
            raise OSError(ENODEV, "no identity")
        return real_fstat(fd)

    mocker.patch("filelock._soft.os.fstat", autospec=True, side_effect=no_identity)
    lock: Final = SoftFileLock(lock_path)
    with pytest.raises(OSError, match="no identity"):
        lock.acquire()
    assert (lock.is_locked, lock_path.exists()) == (False, True)


def test_transient_identity_failure_still_cleans_the_marker(lock_path: Path, mocker: MockerFixture) -> None:
    # Only the first read of the new marker's identity fails. The rollback reads it again through the descriptor it
    # still owns, so on Windows it recognizes the marker it created; an empty marker left there is never broken.
    real_fstat: Final = os.fstat
    calls: Final = itertools.count()

    def fail_first(fd: int) -> os.stat_result:
        if next(calls) == 0:
            raise OSError(EIO, "Input/output error")
        return real_fstat(fd)

    mocker.patch("filelock._soft.os.fstat", side_effect=fail_first)
    lock: Final = SoftFileLock(lock_path)
    with pytest.raises(OSError, match="Input/output error"):
        lock.acquire()
    assert (lock.is_locked, lock_path.exists()) == (False, False)


@pytest.mark.skipif(sys.platform == "win32", reason="Windows cannot exchange two names")
@pytest.mark.usefixtures("unchanged_past_grace")
@NEEDS_STALE_RECLAIM
def test_release_under_a_peers_exchange_leaves_no_marker_naming_this_process(  # pragma: win32 no cover
    lock_path: Path,
) -> None:
    lock: Final = SoftFileLock(lock_path)
    lock.acquire()
    (peer := lock_path.with_name("peer")).write_text(_holder(_DEAD_PID), encoding="utf-8")
    exchange_names(str(lock_path), str(peer))
    lock.release()
    exchange_names(str(lock_path), str(peer))
    restored: Final = lock_path.read_text(encoding="utf-8")

    contender: Final = SoftFileLock(lock_path)
    for _ in range(2):
        with suppress(Timeout):
            contender.acquire(blocking=False)
    assert (restored.startswith(f"{os.getpid()}\n"), contender.is_locked) == (False, True)


@pytest.mark.skipif(sys.platform == "win32", reason="Windows cannot exchange two names")
@NEEDS_STALE_RECLAIM
def test_release_under_a_peers_exchange_leaves_the_peers_marker(lock_path: Path) -> None:  # pragma: win32 no cover
    lock: Final = SoftFileLock(lock_path)
    lock.acquire()
    (peer := lock_path.with_name("peer")).write_text(_holder(_DEAD_PID), encoding="utf-8")
    exchange_names(str(lock_path), str(peer))
    lock.release()

    assert (lock_path.read_text(encoding="utf-8"), peer.read_text(encoding="utf-8")) == (
        _holder(_DEAD_PID),
        "filelock/released\n",
    )


def test_release_without_identity_skips_unlink(lock_path: Path, mocker: MockerFixture) -> None:
    lock = SoftFileLock(lock_path)
    lock.acquire()
    held: Final = lock_path.stat()
    real_fstat: Final = os.fstat

    def no_identity(fd: int) -> os.stat_result:
        # The held descriptor can no longer be identified, so release cannot prove the path is still ours: it closes
        # and clears held state without unlinking rather than risk deleting a successor's marker.
        if os.path.samestat(found := real_fstat(fd), held):
            raise OSError
        return found

    mocker.patch("filelock._soft.os.fstat", side_effect=no_identity)
    lock.release()
    assert not lock.is_locked
    assert lock_path.exists()


def test_normal_release_removes_own_marker(lock_path: Path) -> None:
    with SoftFileLock(lock_path):
        assert lock_path.exists()
    assert not lock_path.exists()


def test_del_suppresses_a_release_error(lock_path: Path, mocker: MockerFixture) -> None:
    # A dropped, still-held lock is finalized by __del__. A release error there must not escape as an unraisable
    # exception during garbage collection, which pytest would fail on and attribute to an unrelated test. Finalizing
    # under a raising release and forcing collection here leaves the run clean only if __del__ swallows the error.
    lock = SoftFileLock(lock_path)
    lock.acquire()
    mocker.patch.object(SoftFileLock, "release", side_effect=OSError("release failed"))
    del lock
    gc.collect()


@NEEDS_UNLINK_OPEN_FILE
def test_stale_release_spares_a_successor(lock_path: Path) -> None:  # pragma: needs unlink-open-file
    holder = SoftFileLock(lock_path)
    holder.acquire()
    # A peer breaks the stale marker and installs its own at the same path; unlink first so it gets a fresh inode.
    lock_path.unlink()
    lock_path.write_text(_holder(_DEAD_PID), encoding="utf-8")
    holder.release()
    assert lock_path.exists()
    assert str(_DEAD_PID) in lock_path.read_text(encoding="utf-8")


@_WINDOWS_ONLY
def test_windows_release_spares_a_replacement(lock_path: Path, mocker: MockerFixture) -> None:  # pragma: win32 cover
    lock = SoftFileLock(lock_path)
    lock.acquire()
    _hold_reports_foreign_identity(mocker)
    lock.release()
    assert lock_path.exists()
    lock_path.unlink()


@pytest.mark.parametrize(
    ("policy", "surfaces"),
    [
        pytest.param("default", True, id="default"),
        pytest.param("raise", True, id="raise"),
        pytest.param("suppress", False, id="suppress"),
    ],
)
def test_close_error_policy_cleans_marker(
    lock_path: Path,
    mocker: MockerFixture,
    policy: CloseErrorPolicy,
    *,
    surfaces: bool,
) -> None:
    lock = SoftFileLock(lock_path, close_error_policy=policy)
    lock.acquire()
    with _close_after_commit(mocker, lock) as (close_error, attempts):
        if surfaces:
            with pytest.raises(OSError, match="close failed") as info:
                lock.release()
            assert info.value is close_error
        else:
            lock.release()
    assert (len(attempts), lock.is_locked, lock.lock_counter, lock_path.exists()) == (1, False, 0, False)


@pytest.mark.parametrize(
    ("depth", "force"),
    [pytest.param(2, False, id="nested"), pytest.param(2, True, id="forced")],
)
def test_close_error_suppression_releases_once(
    lock_path: Path,
    mocker: MockerFixture,
    depth: int,
    *,
    force: bool,
) -> None:
    lock = SoftFileLock(lock_path, close_error_policy="suppress")
    for _acquisition in range(depth):
        lock.acquire()
    with _close_after_commit(mocker, lock) as (_, attempts):
        if force:
            lock.release(force=True)
        else:
            for _release in range(depth):
                lock.release()
    assert (len(attempts), lock.is_locked, lock.lock_counter, lock_path.exists()) == (1, False, 0, False)


@pytest.mark.parametrize(
    "use_proxy",
    [pytest.param(False, id="direct"), pytest.param(True, id="proxy")],
)
def test_context_surfaces_close_error_after_cleanup(
    lock_path: Path,
    mocker: MockerFixture,
    *,
    use_proxy: bool,
) -> None:
    lock = SoftFileLock(lock_path)
    with (
        _close_after_commit(mocker, lock) as (close_error, attempts),
        pytest.raises(OSError, match="close failed") as info,
        lock.acquire() if use_proxy else lock,
    ):
        pass
    assert (info.value, len(attempts), lock.is_locked, lock_path.exists()) == (close_error, 1, False, False)


def test_second_release_does_not_close_reused_descriptor(
    lock_path: Path,
    tmp_path: Path,
    mocker: MockerFixture,
) -> None:
    lock = SoftFileLock(lock_path)
    lock.acquire()
    with (
        _close_after_commit(mocker, lock) as (close_error, attempts),
        pytest.raises(OSError, match="close failed") as info,
    ):
        lock.release()
    assert info.value is close_error
    reused_fd = os.open(tmp_path / "reused", os.O_CREAT | os.O_WRONLY)
    assert reused_fd == attempts[0]
    try:
        lock.release()
        os.fstat(reused_fd)
    finally:
        os.close(reused_fd)


@NEEDS_UNLINK_OPEN_FILE  # pragma: needs unlink-open-file
def test_close_error_spares_successor_marker(lock_path: Path, mocker: MockerFixture) -> None:
    holder = SoftFileLock(lock_path)
    holder.acquire()
    lock_path.unlink()
    lock_path.write_text(_holder(_DEAD_PID), encoding="utf-8")
    with _close_after_commit(mocker, holder) as (_, _attempts), pytest.raises(OSError, match="close failed"):
        holder.release()
    assert lock_path.read_text(encoding="utf-8") == _holder(_DEAD_PID)


@NEEDS_UNLINK_OPEN_FILE  # pragma: needs unlink-open-file
def test_released_record_write_error_spares_successor_marker(lock_path: Path, mocker: MockerFixture) -> None:
    holder: Final = SoftFileLock(lock_path)
    holder.acquire()
    lock_path.unlink()
    lock_path.write_text(_holder(_DEAD_PID), encoding="utf-8")
    mocker.patch("filelock._soft.os.pwrite", side_effect=PermissionError(EACCES, "record denied"))

    holder.release()

    assert not holder.is_locked
    assert lock_path.read_text(encoding="utf-8") == _holder(_DEAD_PID)


@pytest.mark.skipif(sys.platform == "win32", reason="Windows closes the descriptor before it can unlink the marker")
def test_marker_cleanup_failure_keeps_the_lock_held_for_a_retry(  # pragma: win32 no cover
    lock_path: Path, mocker: MockerFixture
) -> None:
    lock: Final = SoftFileLock(lock_path)
    lock.acquire()
    cleanup_error: Final = RuntimeError("cleanup failed")
    unlink: Final = mocker.patch("filelock._soft.Path.unlink", autospec=True, side_effect=cleanup_error)
    with pytest.raises(RuntimeError) as info:
        lock.release()
    held: Final = (lock.is_locked, lock_path.exists())
    mocker.stop(unlink)

    lock.release()

    assert (info.value, held, lock.is_locked, lock_path.exists()) == (cleanup_error, (True, True), False, False)


@pytest.mark.skipif(sys.platform == "win32", reason="Windows always unlinks the marker after the close")
def test_marker_unlink_refused_while_open_is_retried_after_the_close(  # pragma: win32 no cover
    lock_path: Path, mocker: MockerFixture
) -> None:
    lock: Final = SoftFileLock(lock_path)
    lock.acquire()
    real_unlink: Final = Path.unlink
    attempts: Final[list[Path]] = []

    def refuse_while_open(path: Path, missing_ok: bool = False) -> None:
        attempts.append(path)
        if len(attempts) == 1:
            raise PermissionError(EACCES, "file in use")
        real_unlink(path, missing_ok=missing_ok)

    mocker.patch.object(Path, "unlink", autospec=True, side_effect=refuse_while_open)
    lock.release()

    assert (lock.is_locked, lock_path.exists(), len(attempts)) == (False, False, 2)


@pytest.mark.skipif(sys.platform == "win32", reason="Windows always unlinks the marker after the close")
def test_marker_unlink_retried_after_the_close_spares_a_successor(  # pragma: win32 no cover
    lock_path: Path, mocker: MockerFixture
) -> None:
    lock: Final = SoftFileLock(lock_path)
    lock.acquire()

    def refuse_while_open_and_successor_replaces(path: Path, missing_ok: bool = False) -> None:
        (successor := path.with_name("successor")).write_text("live", encoding="utf-8")
        successor.replace(path)
        raise PermissionError(EACCES, f"file in use, missing_ok={missing_ok}")

    mocker.patch.object(Path, "unlink", autospec=True, side_effect=refuse_while_open_and_successor_replaces)
    lock.release()
    mocker.stopall()

    assert (lock.is_locked, lock_path.read_text(encoding="utf-8")) == (False, "live")


@pytest.mark.skipif(sys.platform != "win32", reason="POSIX unlinks the marker before the close, so both cannot fail")
def test_close_and_marker_cleanup_failures_are_grouped(  # pragma: win32 cover
    lock_path: Path, mocker: MockerFixture
) -> None:
    lock = SoftFileLock(lock_path)
    lock.acquire()
    cleanup_error = RuntimeError("cleanup failed")
    unlink_mock: Final = mocker.patch("filelock._soft.unlink_path", side_effect=cleanup_error)
    with _close_after_commit(mocker, lock) as (close_error, _attempts), pytest.raises(ExceptionGroup) as info:
        lock.release()
    mocker.stop(unlink_mock)
    assert (
        info.value.message,
        info.value.exceptions,
        close_error.__context__,
        cleanup_error.__context__,
        lock.is_locked,
    ) == (
        "lock descriptor close and marker cleanup both failed",
        (close_error, cleanup_error),
        None,
        None,
        False,
    )


@pytest.mark.parametrize(
    "replaced",
    [pytest.param(False, id="own-marker-removed"), pytest.param(True, id="replacement-spared")],
)
def test_publication_rollback_close_failure_unlinks_only_its_marker(
    lock_path: Path, mocker: MockerFixture, *, replaced: bool
) -> None:
    mocker.patch("filelock._util.os.write", side_effect=OSError(ENOSPC, "No space left on device"))
    if replaced:
        _hold_reports_foreign_identity(mocker)
    lock: Final = SoftFileLock(lock_path)
    with (
        _close_after_commit(mocker, lock) as (close_error, attempts),
        pytest.raises(OSError, match="close failed") as info,
    ):
        lock.acquire(timeout=0)
    assert (info.value, len(attempts), lock.is_locked, lock_path.exists()) == (close_error, 1, False, replaced)


def test_close_after_commit_ignores_other_descriptors(lock_path: Path, mocker: MockerFixture) -> None:
    # The patch binds os.close process-wide, so a descriptor that is not the lock's, whether closed on another thread
    # or by a finalizer the collector happens to run on this one, must pass through cleanly and stay out of the
    # recorded attempts.
    lock = SoftFileLock(lock_path)
    lock.acquire()
    closed: list[int] = []

    def close_a_pipe() -> None:
        read_fd, write_fd = os.pipe()
        os.close(read_fd)
        os.close(write_fd)
        closed.extend((read_fd, write_fd))

    with _close_after_commit(mocker, lock) as (_close_error, attempts):
        close_a_pipe()
        worker = threading.Thread(target=close_a_pipe)
        worker.start()
        worker.join()
        with pytest.raises(OSError, match="close failed"):
            lock.release()

    assert (len(closed), len(attempts), attempts[0] in closed) == (4, 1, False)


@contextmanager
def _close_after_commit(mocker: MockerFixture, lock: SoftFileLock) -> Generator[tuple[OSError, list[int]]]:
    real_close = os.close
    close_error = OSError(EINTR, "close failed")
    attempts: list[int] = []
    # close is an attribute of the shared os module, so this patch binds for every close in the process while the
    # window is open, including one a GC-run finalizer (a multiprocessing pipe from an earlier test, say) makes on this
    # very thread. Fail only the descriptor this lock lets go of: it clears its bookkeeping right before the one close
    # attempt, so record the descriptor as it does.
    released: list[int | None] = []
    real_mark_released = lock._mark_descriptor_released

    def mark_released() -> None:
        context: Final = lock._context
        released.append(context.pending_lock_file_fd if context.lock_file_fd is None else context.lock_file_fd)
        real_mark_released()

    def close(fd: int) -> None:
        real_close(fd)
        if fd not in released:
            return
        attempts.append(fd)
        raise close_error

    mocker.patch.object(lock, "_mark_descriptor_released", side_effect=mark_released)
    close_mock = mocker.patch("filelock._soft.os.close", side_effect=close)
    try:
        yield close_error, attempts
    finally:
        mocker.stop(close_mock)


def test_soft_windows_unlink_gives_up_after_every_attempt_is_denied(tmp_path: Path, mocker: MockerFixture) -> None:
    # Windows can still hold a handle just after close, so the marker unlink retries on EACCES. Deny every attempt to
    # prove the retry runs and then stops, and that the denial never escapes to the caller.
    from filelock._soft import _file_identity

    marker = tmp_path / "a"
    marker.write_text("x", encoding="utf-8")
    lock = SoftFileLock(marker)
    mocker.patch("filelock._soft.time.sleep")
    unlink: Final = mocker.patch("filelock._soft.unlink_path", side_effect=PermissionError(EACCES, "handle still open"))

    lock._windows_unlink_if_ours(_file_identity(os.lstat(marker)))
    denied: Final = marker.exists()
    mocker.stop(unlink)

    # The marker is still ours, so the next acquire finishes the cleanup instead of waiting on it.
    with lock.acquire(timeout=0):
        assert denied


@pytest.fixture
def unchanged_past_grace(mocker: MockerFixture) -> None:
    # Each look at the marker lands a full grace window after the previous one on the observer's clock, so an unchanged
    # malformed marker ages out on the second look without the test sleeping through the window.
    mocker.patch("filelock._soft.time.monotonic", side_effect=itertools.count(step=MALFORMED_LOCK_AGE_THRESHOLD))


def test_unparseable_lock_with_an_ancient_mtime_not_evicted_on_first_sight(lock_path: Path) -> None:
    # The mtime carries the writer's or the file server's clock, so an ancient one may be a marker written a moment ago
    # by a host whose clock runs behind; only time watched on this process's own clock ages it out.
    lock_path.write_bytes(b"not-a-pid\n")
    os.utime(lock_path, (0, 0))

    _assert_times_out(lock_path)


@pytest.mark.parametrize(
    "content",
    [
        pytest.param("1_000\nhost\n", id="pid-underscore"),
        pytest.param("\u0661\u0662\u0663\nhost\n", id="pid-arabic-indic-digits"),
        pytest.param("+1000\nhost\n", id="pid-sign"),
        pytest.param("1000\nhost\n1_0\n", id="start-underscore"),
    ],
)
def test_lock_with_a_non_decimal_number_names_no_holder(lock_path: Path, content: str) -> None:
    # int() reads each of these as a number, but no filelock writes them, so they spell a malformed lock, not a PID.
    lock_path.write_text(content, encoding="utf-8")

    assert SoftFileLock(lock_path).pid is None


@_WINDOWS_ONLY  # pragma: win32 cover
@pytest.mark.parametrize("oplocked_lock_path", [_holder(os.getpid())], indirect=True, ids=["live-marker"])
def test_soft_contender_keeps_its_deadline_under_an_oplocked_live_marker(oplocked_lock_path: Path) -> None:
    lock = SoftFileLock(oplocked_lock_path, is_singleton=False)

    finished, error = call_within(lambda: lock.acquire(timeout=0.1), 5)

    assert (finished, type(error)) == (True, Timeout)


@_WINDOWS_ONLY  # pragma: win32 cover
def test_stale_break_keeps_its_deadline_under_an_oplocked_marker(
    lock_path: Path, hold_oplock: Callable[[Path], Callable[[], None]]
) -> None:
    lock_path.write_text(_holder(_DEAD_PID), encoding="utf-8")
    close_holder = hold_oplock(lock_path)
    lock = SoftFileLock(lock_path, is_singleton=False)

    finished, error = call_within(lambda: lock.acquire(timeout=0.1), 5)
    close_holder()

    assert (finished, type(error)) == (True, Timeout)
    with lock.acquire(timeout=5):
        assert lock.is_locked
