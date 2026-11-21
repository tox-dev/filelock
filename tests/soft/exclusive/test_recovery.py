from __future__ import annotations

import os
import sys
from errno import ENODEV, EPERM
from typing import TYPE_CHECKING, Final

import pytest

from filelock import MarkerSoftFileLock, SoftFileLease, SoftFileLock, Timeout
from filelock._identity import host_name, process_start_token
from filelock._soft._records import (
    _MAX_LOCK_FILE_SIZE,
)
from tests.capability_marks import (
    NEEDS_POSIX_SIGNALS,
    NEEDS_STALE_RECLAIM,
)
from tests.conftest import STALE_BREAKERS, OplockHolder, acquire_after_reclaim
from tests.pid_namespace_helpers import pin_pid_namespace
from tests.process_helpers import call_within

if TYPE_CHECKING:
    from pathlib import Path

    from pytest_mock import MockerFixture

from tests.soft.exclusive.recovery_helpers import (
    DEAD_PID,
    HOST,
    REQUIRES_START_TOKEN,
    WINDOWS_ONLY,
    assert_self_heals,
    assert_times_out,
    holder_record,
    recovery_name,
)


@pytest.mark.parametrize(
    "content",
    [
        pytest.param(holder_record(DEAD_PID), id="two_line"),
        pytest.param(holder_record(DEAD_PID, start=123456789), id="three_line"),
    ],
)
@NEEDS_STALE_RECLAIM
def test_stale_lock_broken_when_process_dead(lock_path: Path, mocker: MockerFixture, content: str) -> None:
    lock_path.write_text(content, encoding="utf-8")
    mocker.patch("filelock._identity._process.process_alive", return_value=False, autospec=True)
    assert_self_heals(lock_path)


def test_stale_lock_not_broken_when_process_alive(lock_path: Path, mocker: MockerFixture) -> None:
    lock_path.write_text(holder_record(os.getpid()), encoding="utf-8")
    mocker.patch("filelock._identity._process.process_alive", return_value=True, autospec=True)
    assert_times_out(lock_path)


def test_stale_lock_not_broken_different_hostname(lock_path: Path) -> None:
    marker = holder_record(DEAD_PID, host="other-host.example.com")
    lock_path.write_text(marker, encoding="utf-8")
    lock = SoftFileLock(lock_path)
    acquire_after_reclaim(lock)
    assert (lock.is_locked, lock_path.read_text(encoding="utf-8")) == (False, marker)


@pytest.mark.parametrize(
    ("lock_type", "record"),
    [
        pytest.param(SoftFileLock, holder_record(DEAD_PID, host="other-host.example.com"), id="soft"),
        pytest.param(MarkerSoftFileLock, holder_record(DEAD_PID, host="other-host.example.com"), id="marker-v1"),
        pytest.param(
            MarkerSoftFileLock,
            f"filelock/2\npid={DEAD_PID}\nhost=other-host.example.com\nmode=exclusive\n",
            id="marker-v2",
        ),
    ],
)
def test_timeout_names_a_holder_on_another_host(lock_path: Path, lock_type: type[SoftFileLock], record: str) -> None:
    lock_path.write_text(record, encoding="utf-8")
    with pytest.raises(Timeout, match=f"held by pid {DEAD_PID} on other-host.example.com, which this host cannot"):
        lock_type(lock_path, timeout=0).acquire()


@pytest.mark.parametrize(
    ("lock_type", "record"),
    [
        pytest.param(SoftFileLock, holder_record(DEAD_PID, host="other\x1b[2K\x1b[1A\x7fhost"), id="soft"),
        pytest.param(
            MarkerSoftFileLock,
            f"filelock/2\npid={DEAD_PID}\nhost=other\x1b[2K\x1b[1A\x7fhost\nmode=exclusive\n",
            id="marker-v2",
        ),
    ],
)
def test_timeout_escapes_a_holder_hostname_outside_the_grammar(
    lock_path: Path, lock_type: type[SoftFileLock], record: str
) -> None:
    lock_path.write_text(record, encoding="utf-8")
    with pytest.raises(Timeout) as caught:
        lock_type(lock_path, timeout=0).acquire()
    assert f"held by pid {DEAD_PID} on other?1b[2K?1b[1A?7fhost, which this host cannot" in str(caught.value)


def test_timeout_leaves_out_a_holder_on_this_host(lock_path: Path) -> None:
    with SoftFileLock(lock_path), pytest.raises(Timeout) as caught:
        SoftFileLock(lock_path, timeout=0).acquire()
    assert str(caught.value) == f"The file lock '{lock_path}' could not be acquired."


@REQUIRES_START_TOKEN
def test_recycled_pid_marker_self_heals(lock_path: Path) -> None:
    # A start-token mismatch distinguishes a recycled PID from its previous owner.
    token = process_start_token(os.getpid())
    assert token is not None
    lock_path.write_text(holder_record(os.getpid(), start=token + 1), encoding="utf-8")
    assert_self_heals(lock_path)


@pytest.mark.skipif(  # pragma: linux cover
    sys.platform not in {"linux", "android"}, reason="PID namespaces exist only on Linux"
)
@REQUIRES_START_TOKEN
@pytest.mark.parametrize(
    ("contender_namespace", "reclaimed"),
    [pytest.param(0xF0000001, True, id="same-namespace"), pytest.param(0xF0000002, False, id="sibling-namespace")],
)
def test_recycled_pid_marker_self_heals_only_in_its_pid_namespace(
    lock_path: Path, mocker: MockerFixture, contender_namespace: int, *, reclaimed: bool
) -> None:
    token: Final[int | None] = process_start_token(os.getpid())
    assert token is not None
    pin_pid_namespace(mocker, 0xF0000001)
    lock_path.write_text(holder_record(os.getpid(), host=host_name(), start=token + 1), encoding="utf-8")
    pin_pid_namespace(mocker, contender_namespace)

    lock: Final[SoftFileLock] = SoftFileLock(lock_path)
    acquire_after_reclaim(lock)
    assert lock.is_locked is reclaimed
    lock.release()


@REQUIRES_START_TOKEN
def test_live_process_with_matching_start_token_retained(lock_path: Path) -> None:
    # Our own PID and our own start token: the recorded owner is this live process, so the marker must stand.
    lock_path.write_text(holder_record(os.getpid(), start=process_start_token(os.getpid())), encoding="utf-8")
    assert_times_out(lock_path)


def test_live_process_without_start_token_falls_back_to_pid(lock_path: Path) -> None:
    # A two-line marker carries no start token, so liveness rests on the PID alone; a live PID is left held.
    lock_path.write_text(holder_record(os.getpid()), encoding="utf-8")
    assert_times_out(lock_path)


@NEEDS_POSIX_SIGNALS  # pragma: needs posix-signals
@pytest.mark.parametrize(
    "errno",
    [pytest.param(EPERM, id="eperm"), pytest.param(ENODEV, id="unexpected_device")],
)
def test_stale_lock_not_broken_on_kill_error(lock_path: Path, mocker: MockerFixture, errno: int) -> None:
    lock_path.write_text(holder_record(99999), encoding="utf-8")
    mocker.patch("filelock._identity._process.os.kill", side_effect=OSError(errno, "kill failed"), autospec=True)
    assert_times_out(lock_path)


@pytest.mark.usefixtures("unchanged_past_grace")
@pytest.mark.parametrize("lock_type", [SoftFileLock, MarkerSoftFileLock, SoftFileLease])
def test_empty_lock_not_evicted_when_old(lock_path: Path, lock_type: type[SoftFileLock]) -> None:
    # A SoftFileLock before 3.22 writes nothing into its marker and holds it for as long as it runs.
    lock_path.write_bytes(b"")
    contender: Final[SoftFileLock] = lock_type(lock_path)
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
    recovery_name(lock).write_bytes(b"")
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
        pytest.param(f"{DEAD_PID}\nhostname\nnot-a-token\n".encode(), id="three_line_bad_start_token"),
        pytest.param(f"{DEAD_PID}\nother-ho".encode(), id="truncated_hostname"),
    ],
)
@pytest.mark.usefixtures("unchanged_past_grace")
@NEEDS_STALE_RECLAIM
def test_unparseable_lock_evicted_when_old(lock_path: Path, content: bytes) -> None:
    lock_path.write_bytes(content)
    assert_self_heals(lock_path)


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
    assert_times_out(lock_path)


@pytest.mark.skipif(sys.platform == "win32", reason="st_ctime is the creation time on Windows, which no change moves")
@pytest.mark.usefixtures("unchanged_past_grace")
def test_unparseable_lock_changed_under_its_old_mtime_and_inode_not_evicted(  # pragma: win32 no cover
    lock_path: Path,
) -> None:
    # Another file can repeat both: its owner sets the mtime, and ext4 hands a freed inode number to the next file.
    lock_path.write_bytes(b"not-a-pid\n")
    watched: Final[os.stat_result] = lock_path.stat()
    contender: Final[SoftFileLock] = SoftFileLock(lock_path)
    with pytest.raises(Timeout):
        contender.acquire(blocking=False)
    while lock_path.stat().st_ctime_ns == watched.st_ctime_ns:
        os.utime(lock_path, ns=(watched.st_atime_ns, watched.st_mtime_ns))
    with pytest.raises(Timeout):
        contender.acquire(blocking=False)
    assert lock_path.exists()


@REQUIRES_START_TOKEN
def test_fresh_marker_with_truncated_start_token_not_evicted(lock_path: Path) -> None:
    # A reader racing our own write sees a cut start token, which must not pass for a recycled PID and break our lock.
    lock_path.write_text(f"{os.getpid()}\n{HOST}\n{process_start_token(os.getpid())}"[:-1], encoding="utf-8")
    assert_times_out(lock_path)


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
    lock_path.write_text(holder_record(pid), encoding="utf-8")
    # Zero and negative PIDs can name process groups. Oversized PIDs overflow the OS probe.
    assert_self_heals(lock_path)


def test_out_of_range_pid_not_evicted_when_fresh(lock_path: Path) -> None:
    lock_path.write_text(holder_record(0), encoding="utf-8")
    # A peer may still be writing a fresh marker.
    assert_times_out(lock_path)


def test_unparseable_lock_with_an_ancient_mtime_not_evicted_on_first_sight(lock_path: Path) -> None:
    # A file server's clock can run behind the observing host.
    lock_path.write_bytes(b"not-a-pid\n")
    os.utime(lock_path, (0, 0))

    assert_times_out(lock_path)


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


@WINDOWS_ONLY  # pragma: win32 cover
@pytest.mark.parametrize(
    "oplocked_lock_path", [pytest.param(holder_record(os.getpid()), id="live-marker")], indirect=True
)
def test_soft_contender_keeps_its_deadline_under_an_oplocked_live_marker(oplocked_lock_path: Path) -> None:
    lock = SoftFileLock(oplocked_lock_path, is_singleton=False)

    finished, error = call_within(lambda: lock.acquire(timeout=0.1), 5)

    assert (finished, type(error)) == (True, Timeout)


@WINDOWS_ONLY  # pragma: win32 cover
@pytest.mark.parametrize("read_handle", [pytest.param(False, id="level-1"), pytest.param(True, id="read-handle")])
def test_stale_break_keeps_its_deadline_under_an_oplocked_marker(
    lock_path: Path, hold_oplock: OplockHolder, *, read_handle: bool
) -> None:
    # A read-handle oplock permits the open and blocks the rename.
    lock_path.write_text(holder_record(DEAD_PID), encoding="utf-8")
    close_holder = hold_oplock(lock_path, read_handle=read_handle)
    lock = SoftFileLock(lock_path, is_singleton=False)

    finished, error = call_within(lambda: lock.acquire(timeout=0.1), 5)
    close_holder()

    assert (finished, type(error)) == (True, Timeout)
    with lock.acquire(timeout=5):
        assert lock.is_locked
