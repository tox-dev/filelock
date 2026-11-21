from __future__ import annotations

import os
import sys
from errno import EACCES, EINVAL, EIO, ELOOP, ESRCH
from pathlib import Path
from typing import TYPE_CHECKING, Final

import pytest

from filelock import SoftFileLock, Timeout
from filelock._filesystem import exchange_names
from tests.capability_marks import NEEDS_STALE_RECLAIM
from tests.conftest import DEAD_HOLDER, STALE_BREAKERS
from tests.filesystem.conftest import rewrite_in_place

if TYPE_CHECKING:
    from collections.abc import Callable

    from pytest_mock import MockerFixture


@pytest.mark.skipif(sys.platform == "win32", reason="the liveness probe of a Windows PID does not call os.kill")
@STALE_BREAKERS
def test_stale_break_spares_a_successor_on_the_freed_inode_number(  # pragma: win32 no cover
    tmp_path: Path,
    mocker: MockerFixture,
    reuse_inode: Callable[[Path, os.stat_result], None],
    lock_type: type[SoftFileLock],
    record: str,
) -> None:
    (lock := tmp_path / "test.lock").write_text(record, encoding="utf-8")
    stale: Final[os.stat_result] = lock.lstat()

    def owner_gone_and_successor_acquired(pid: int, signal: int) -> None:
        # A replacement can reuse the stale inode number in the same timestamp tick.
        lock.unlink()
        lock.write_text("live", encoding="utf-8")
        os.utime(lock, ns=(stale.st_atime_ns, stale.st_mtime_ns))
        reuse_inode(lock, stale)
        raise ProcessLookupError(ESRCH, f"no process {pid} for signal {signal}")

    mocker.patch("filelock._identity._process.os.kill", side_effect=owner_gone_and_successor_acquired, autospec=True)
    with pytest.raises(Timeout):
        lock_type(lock).acquire(blocking=False)
    mocker.stopall()

    assert lock.read_text(encoding="utf-8") == "live"


@pytest.mark.skipif(sys.platform == "win32", reason="Windows cannot rename a file held open, so it pins nothing")
@STALE_BREAKERS
def test_stale_break_skips_a_marker_swapped_in_after_the_pin(  # pragma: win32 no cover
    tmp_path: Path, mocker: MockerFixture, lock_type: type[SoftFileLock], record: str
) -> None:
    (lock := tmp_path / "test.lock").write_text(record, encoding="utf-8")
    fstat = os.fstat
    swapped: list[bool] = []

    def pin_then_peer_swaps_the_marker(fd: int) -> os.stat_result:
        found: Final[os.stat_result] = fstat(fd)
        if not swapped:
            swapped.append(True)
            (replacement := lock.with_name("replacement")).write_text(record, encoding="utf-8")
            replacement.replace(lock)
        return found

    mocker.patch("os.fstat", side_effect=pin_then_peer_swaps_the_marker, autospec=True)
    rename = mocker.spy(Path, "rename")
    with pytest.raises(Timeout):
        lock_type(lock).acquire(blocking=False)
    mocker.stopall()

    # The filesystem can reuse an unpinned inode number before the break.
    assert (lock.read_text(encoding="utf-8"), rename.call_count) == (record, 0)


@pytest.mark.skipif(sys.platform == "win32", reason="Windows cannot rename a file held open, so it pins nothing")
@STALE_BREAKERS
@NEEDS_STALE_RECLAIM
def test_stale_break_reclaims_a_marker_it_cannot_pin(  # pragma: win32 no cover
    tmp_path: Path, mocker: MockerFixture, lock_type: type[SoftFileLock], record: str
) -> None:
    # Without O_PATH, the fallback cannot pin symlinks or sockets.
    (lock := tmp_path / "test.lock").write_text(record, encoding="utf-8")
    open_ = os.open
    pinned: list[bool] = []

    def pin_refused(path: str, flags: int, mode: int = 0o777, *, dir_fd: int | None = None) -> int:
        if os.fspath(path) == os.fspath(lock) and not flags & os.O_CREAT and not pinned:
            pinned.append(True)
            raise OSError(ELOOP, "Too many levels of symbolic links", os.fspath(path))
        return open_(path, flags, mode, dir_fd=dir_fd)

    mocker.patch("os.open", side_effect=pin_refused, autospec=True)
    with lock_type(lock, timeout=1) as held:
        assert held.is_locked


@pytest.mark.skipif(sys.platform == "win32", reason="Windows does not open the marker while pinning")
@STALE_BREAKERS
def test_stale_break_lets_acquire_retry_after_the_marker_vanishes_before_the_pin(  # pragma: win32 no cover
    tmp_path: Path, mocker: MockerFixture, lock_type: type[SoftFileLock], record: str
) -> None:
    (lock := tmp_path / "test.lock").write_text(record, encoding="utf-8")
    open_ = os.open
    released: list[bool] = []

    def holder_releases_then_open(path: str, flags: int, mode: int = 0o777, *, dir_fd: int | None = None) -> int:
        if os.fspath(path) == os.fspath(lock) and not flags & os.O_CREAT and not released:
            released.append(True)
            lock.unlink()
        return open_(path, flags, mode, dir_fd=dir_fd)

    mocker.patch("os.open", side_effect=holder_releases_then_open, autospec=True)
    with lock_type(lock, timeout=1) as held:
        assert held.is_locked


@pytest.mark.usefixtures("break_route")
def test_break_lock_file_lets_acquire_retry_after_the_marker_vanishes(
    stale_lock: Path, on_break_recheck: Callable[[Path, Callable[[], None]], None]
) -> None:
    on_break_recheck(stale_lock, stale_lock.unlink)
    with SoftFileLock(stale_lock, timeout=1):
        assert stale_lock.read_text(encoding="utf-8").splitlines()[0] == str(os.getpid())


@pytest.mark.skipif(sys.platform == "win32", reason="Windows cannot exchange two names")
@pytest.mark.filterwarnings("ignore::filelock.SoftFileLockLifetimeWarning")
@pytest.mark.parametrize(
    "lifetime",
    [pytest.param(None, id="stale-take-over"), pytest.param(0.1, id="expiry-placeholder")],
)
@NEEDS_STALE_RECLAIM
def test_stale_take_over_keeps_the_lock_when_a_rename_replaces_its_marker(  # pragma: win32 no cover
    stale_lock: Path, mocker: MockerFixture, lifetime: float | None
) -> None:
    os.utime(stale_lock, (0, 0))
    lstat = os.lstat
    replaced: list[bool] = []

    def peer_replaces_the_swapped_in_file(path: str | os.PathLike[str], *, dir_fd: int | None = None) -> os.stat_result:
        found: Final[os.stat_result] = lstat(path, dir_fd=dir_fd)
        if ".break." in os.fspath(path) and not replaced:
            replaced.append(True)
            (other := stale_lock.with_name("other")).write_text("other", encoding="utf-8")
            other.replace(stale_lock)
        return found

    mocker.patch.object(os, "lstat", new=peer_replaces_the_swapped_in_file)
    lock: Final[SoftFileLock] = SoftFileLock(stale_lock, lifetime=lifetime)
    lock.acquire(blocking=False)
    mocker.stopall()
    held: Final[bool] = lock.is_locked
    lock.release()

    assert (held, stale_lock.read_text(encoding="utf-8"), list(stale_lock.parent.glob("*.break.*"))) == (
        True,
        "other",
        [],
    )


@pytest.mark.skipif(sys.platform == "win32", reason="Windows cannot exchange two names")
@NEEDS_STALE_RECLAIM
def test_remove_own_file_placeholder_names_this_process(
    tmp_path: Path, mocker: MockerFixture
) -> None:  # pragma: win32 no cover
    mocker.patch("os.fchmod", side_effect=OSError(EIO, "Input/output error"), autospec=True)
    exchange = exchange_names
    placeholders: Final[list[bytes]] = []

    def record_the_placeholder(source: str, target: str) -> bool:
        placeholders.append(Path(source).read_bytes())
        return exchange(source, target)

    mocker.patch("filelock._filesystem._exchange.exchange_names", side_effect=record_the_placeholder, autospec=True)
    with pytest.raises(OSError, match="Input/output error"):
        SoftFileLock(tmp_path / "test.lock", mode=0o600).acquire()

    assert placeholders == [f"filelock/break\n{os.getpid()}\n".encode()]


@pytest.mark.skipif(sys.platform == "win32", reason="Windows cannot exchange two names")
@STALE_BREAKERS
@NEEDS_STALE_RECLAIM
def test_stale_break_takes_the_lock_over_in_one_poll(  # pragma: win32 no cover
    tmp_path: Path, lock_type: type[SoftFileLock], record: str
) -> None:
    (lock := tmp_path / "test.lock").write_text(record, encoding="utf-8")
    contender: Final[SoftFileLock] = lock_type(lock)
    contender.acquire(blocking=False)

    assert (contender.is_lock_held_by_us, list(tmp_path.glob("test.lock.break.*"))) == (True, [])
    contender.release()


@pytest.mark.skipif(sys.platform == "win32", reason="Windows pins nothing")
def test_stale_break_spares_a_successor_on_the_freed_inode_number_across_the_exchange(  # pragma: win32 no cover
    stale_lock: Path,
    mocker: MockerFixture,
    reuse_inode: Callable[[Path, os.stat_result], None],
    on_break_recheck: Callable[[Path, Callable[[], None]], None],
) -> None:
    stale: Final[os.stat_result] = stale_lock.lstat()

    def successor_takes_the_freed_number() -> None:
        # Only the pinned inode distinguishes a same-tick replacement with a reused inode number.
        stale_lock.unlink()
        stale_lock.write_text("live", encoding="utf-8")
        os.utime(stale_lock, ns=(stale.st_atime_ns, stale.st_mtime_ns))
        reuse_inode(stale_lock, stale)

    on_break_recheck(stale_lock, successor_takes_the_freed_number)
    with pytest.raises(Timeout):
        SoftFileLock(stale_lock).acquire(blocking=False)
    mocker.stopall()

    assert (stale_lock.read_text(encoding="utf-8"), list(stale_lock.parent.glob("test.lock.break.*"))) == ("live", [])


@pytest.mark.skipif(sys.platform != "win32", reason="only Windows renames a marker aside")
def test_break_lock_file_leaves_a_recreated_marker_aside_where_links_are_refused(  # pragma: win32 cover
    stale_lock: Path, mocker: MockerFixture, on_break_recheck: Callable[[Path, Callable[[], None]], None]
) -> None:
    mocker.patch("os.link", side_effect=PermissionError(1, "Operation not permitted"), autospec=True)
    on_break_recheck(stale_lock, lambda: rewrite_in_place(stale_lock))
    with pytest.raises(Timeout):
        SoftFileLock(stale_lock).acquire(blocking=False)
    mocker.stopall()

    assert [path.read_text(encoding="utf-8") for path in stale_lock.parent.glob("test.lock.break.*")] == ["live"]


@pytest.mark.skipif(sys.platform == "win32", reason="Windows cannot exchange two names")
@pytest.mark.parametrize(
    "error",
    [
        pytest.param(EINVAL, id="filesystem-without-exchange"),
        pytest.param(EACCES, id="exchange-denied"),
    ],
)
def test_break_lock_file_leaves_the_marker_where_the_exchange_fails(  # pragma: win32 no cover
    stale_lock: Path, mocker: MockerFixture, error: int
) -> None:
    # On NFS and SMB, renaming instead of exchanging could expose an empty lock path.
    renamer = mocker.create_autospec(_exchange_call, return_value=error)
    mocker.patch("filelock._filesystem._exchange._rename_call", return_value=(renamer, 2), autospec=True)
    with pytest.raises(Timeout):
        SoftFileLock(stale_lock).acquire(blocking=False)

    assert (
        stale_lock.read_text(encoding="utf-8"),
        list(stale_lock.parent.glob("test.lock.break.*")),
        renamer.called,
    ) == (
        DEAD_HOLDER,
        [],
        True,
    )


def _exchange_call(source: bytes, target: bytes, flags: int) -> int:
    raise NotImplementedError  # pragma: no cover  # the autospec answers the call, so only the signature matters


@pytest.mark.skipif(sys.platform == "win32", reason="Windows cannot exchange two names")
@NEEDS_STALE_RECLAIM
@pytest.mark.parametrize(
    "expected", [pytest.param({}, id="own-marker"), pytest.param({"test.lock": b"live"}, id="peer-marker")]
)
@pytest.mark.parametrize("inspect", [pytest.param(False, id="exchange"), pytest.param(True, id="identity-check")])
def test_interrupted_rollback_removes_only_its_own_marker(  # pragma: win32 no cover
    tmp_path: Path, mocker: MockerFixture, expected: dict[str, bytes], inspect: bool
) -> None:
    path = tmp_path / "test.lock"
    mocker.patch("os.fchmod", side_effect=OSError(EIO, "cannot set mode"), autospec=True)
    exchange = exchange_names

    def interrupt_check(_path: str) -> os.stat_result:
        mocker.stopall()
        raise KeyboardInterrupt

    def interrupt_after_exchange(source: str, target: str) -> bool:
        mocker.stopall()
        if expected:
            (peer := tmp_path / "peer").write_bytes(b"live")
            peer.replace(path)
        assert exchange(source, target)
        if inspect:
            mocker.patch("os.lstat", autospec=True, side_effect=interrupt_check)
            return True
        raise KeyboardInterrupt

    mocker.patch("filelock._filesystem._exchange.exchange_names", autospec=True, side_effect=interrupt_after_exchange)
    with pytest.raises(KeyboardInterrupt):
        SoftFileLock(path, mode=0o600).acquire(blocking=False)

    assert {entry.name: entry.read_bytes() for entry in tmp_path.iterdir()} == expected


@pytest.mark.skipif(sys.platform == "win32", reason="Windows cannot exchange two names")
@NEEDS_STALE_RECLAIM
def test_stale_take_over_interrupted_before_the_check_puts_the_marker_back(  # pragma: win32 no cover
    stale_lock: Path, mocker: MockerFixture
) -> None:
    # An interrupted exchange can leave a marker for a live PID that holds no lock.
    lstat = os.lstat
    interrupted: list[bool] = []

    def interrupt_after_the_exchange(path: str | os.PathLike[str], *, dir_fd: int | None = None) -> os.stat_result:
        if ".break." in os.fspath(path) and not interrupted:
            interrupted.append(True)
            raise KeyboardInterrupt
        return lstat(path, dir_fd=dir_fd)

    mocker.patch.object(os, "lstat", new=interrupt_after_the_exchange)
    lock: Final[SoftFileLock] = SoftFileLock(stale_lock)
    with pytest.raises(KeyboardInterrupt):
        lock.acquire(blocking=False)
    mocker.stopall()

    assert (
        stale_lock.read_text(encoding="utf-8"),
        list(stale_lock.parent.glob("test.lock.break.*")),
        lock.is_locked,
    ) == (
        DEAD_HOLDER,
        [],
        False,
    )


@NEEDS_STALE_RECLAIM
def test_break_lock_file_break_name_is_unguessable(stale_lock: Path, mocker: MockerFixture) -> None:
    # A predictable per-process break name would let a concurrent breaker replace it before unlink.
    guessable: Final[Path] = stale_lock.with_name(f"test.lock.break.{os.getpid()}")
    lstat = os.lstat

    def peer_moves_a_live_lock_onto_the_guessable_name(path: str | os.PathLike[str]) -> os.stat_result:
        result: Final[os.stat_result] = lstat(path)
        if ".break." in os.fspath(path) and not guessable.exists():
            stale_lock.write_text("live", encoding="utf-8")
            stale_lock.rename(guessable)
        return result

    mocker.patch("os.lstat", side_effect=peer_moves_a_live_lock_onto_the_guessable_name, autospec=True)
    with SoftFileLock(stale_lock):
        pass

    assert guessable.read_text(encoding="utf-8") == "live"
