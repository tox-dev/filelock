from __future__ import annotations

import os
import runpy
import sys
import time
from contextlib import suppress
from errno import EACCES, EINVAL, EIO, ELOOP, ENOENT, EPERM, ESRCH
from pathlib import Path
from typing import TYPE_CHECKING, Final

import pytest

from filelock import MarkerSoftFileLock, SoftFileLease, SoftFileLock, Timeout
from filelock._identity import host_name
from tests.capability_marks import NEEDS_FILE_PERMISSIONS, NEEDS_SYMLINK, NEEDS_UNLINK_OPEN_FILE

if TYPE_CHECKING:
    from collections.abc import Callable, Generator

    from pytest_mock import MockerFixture

_SKIP_AS_ROOT: Final[pytest.MarkDecorator] = pytest.mark.skipif(
    sys.platform != "win32" and os.geteuid() == 0,
    reason="root can write a 0o444 file",
)
# Linux and macOS cap PIDs below 2**22 + 1, and Windows answers OpenProcess for it with an invalid-parameter error, so
# the marker names a holder every platform reads as dead.
_DEAD_HOLDER: Final[str] = f"{2**22 + 1}\n{host_name()}\n"
_DEAD_OWNER: Final[str] = f"filelock/2\npid={2**22 + 1}\nhost={host_name()}\nmode=exclusive\n"


@pytest.fixture
def stale_lock(tmp_path: Path) -> Path:
    path: Final[Path] = tmp_path / "test.lock"
    path.write_text(_DEAD_HOLDER, encoding="utf-8")
    return path


def test_break_lock_file_unlinks_a_dead_holders_marker(stale_lock: Path) -> None:
    with SoftFileLock(stale_lock, timeout=1):
        assert list(stale_lock.parent.glob("test.lock.break.*")) == []


def _rewrite_in_place(lock: Path) -> None:
    lock.write_text("live", encoding="utf-8")
    os.utime(lock, (time.time() + 10, time.time() + 10))


def _replace_with_same_mtime(lock: Path) -> None:
    # A filesystem with coarse modification times (NFS, FAT) gives a same-second recreation the old mtime, so only the
    # inode tells it apart. Writing the replacement while the original exists guarantees a fresh inode.
    stale: Final[os.stat_result] = lock.stat()
    (replacement := lock.with_name("recreated")).write_text("live", encoding="utf-8")
    os.utime(replacement, ns=(stale.st_atime_ns, stale.st_mtime_ns))
    replacement.replace(lock)


@pytest.mark.usefixtures("break_route")
@pytest.mark.parametrize(
    "recreate",
    [pytest.param(_rewrite_in_place, id="mtime-advanced"), pytest.param(_replace_with_same_mtime, id="inode-changed")],
)
def test_break_lock_file_keeps_a_marker_recreated_after_the_recheck(
    stale_lock: Path,
    mocker: MockerFixture,
    on_break_recheck: Callable[[Path, Callable[[], None]], None],
    recreate: Callable[[Path], None],
) -> None:
    on_break_recheck(stale_lock, lambda: recreate(stale_lock))
    with pytest.raises(Timeout):
        SoftFileLock(stale_lock).acquire(blocking=False)
    mocker.stopall()

    assert (stale_lock.read_text(encoding="utf-8"), list(stale_lock.parent.glob("test.lock.break.*"))) == ("live", [])


@pytest.mark.parametrize(
    "recreate",
    [
        pytest.param(_rewrite_in_place, id="mtime-advanced"),
        # The peer replaces the marker while the stale read still holds it open.
        pytest.param(_replace_with_same_mtime, id="inode-changed", marks=NEEDS_UNLINK_OPEN_FILE),
    ],
)
def test_break_lock_file_leaves_a_marker_recreated_since_the_stale_check(
    stale_lock: Path, mocker: MockerFixture, recreate: Callable[[Path], None]
) -> None:
    def read_then_peer_recreates(fd: int, length: int) -> bytes:
        data: Final = real_read(fd, length)
        recreate(stale_lock)
        return data

    real_read: Final = os.read
    mocker.patch("os.read", autospec=True, side_effect=read_then_peer_recreates)
    rename: Final = mocker.spy(Path, "rename")
    with pytest.raises(Timeout):
        SoftFileLock(stale_lock).acquire(blocking=False)
    # Moving the live marker aside would empty the path until it is linked back, long enough for a third holder.
    assert (stale_lock.read_text(encoding="utf-8"), rename.call_count) == ("live", 0)


@pytest.mark.usefixtures("break_route")
@pytest.mark.parametrize(
    "point",
    [
        pytest.param("lstat", id="after-the-recheck-and-the-take"),
        pytest.param("open", id="after-the-placeholder"),
        pytest.param("unlink", id="after-each-removal"),
    ],
)
def test_stale_break_admits_no_third_holder_beside_a_recreated_marker(
    stale_lock: Path, mocker: MockerFixture, on_break_recheck: Callable[[Path, Callable[[], None]], None], point: str
) -> None:
    # A peer broke the stale marker and the next holder acquired after the recheck. A third contender then polls at
    # every step of the break, including between taking the marker and checking whose it is.
    holder: Final = SoftFileLock(stale_lock)
    overlaps: list[bool] = []
    polling: list[bool] = []
    real: Final = getattr(os, point)

    def third_contender_polls(path: str | os.PathLike[str], *args: int, **kwargs: int | None) -> object:
        result: Final = real(path, *args, **kwargs)
        if holder.is_locked and not polling and os.fspath(path).startswith(os.fspath(stale_lock)):
            polling.append(True)
            third: Final = SoftFileLock(stale_lock)
            with suppress(Timeout):
                third.acquire(blocking=False)
            overlaps.append(third.is_locked)
            third.release()
            polling.clear()
        return result

    def peer_breaks_and_holder_acquires() -> None:
        stale_lock.unlink()
        holder.acquire()

    mocker.patch.object(os, point, new=third_contender_polls)
    on_break_recheck(stale_lock, peer_breaks_and_holder_acquires)
    with pytest.raises(Timeout):
        SoftFileLock(stale_lock).acquire(blocking=False)
    mocker.stopall()

    assert (any(overlaps), holder.is_lock_held_by_us) == (False, True)
    holder.release()


@pytest.mark.skipif(sys.platform != "win32", reason="only Windows renames a marker aside")
def test_break_lock_file_put_back_never_replaces_a_marker_at_the_path(  # pragma: win32 cover
    stale_lock: Path, mocker: MockerFixture
) -> None:
    rename: Final = Path.rename

    def peer_recreates_then_third_creates(source: Path, target: str) -> Path:
        _rewrite_in_place(source)
        result: Final[Path] = rename(source, target)
        source.write_text("third", encoding="utf-8")
        return result

    mocker.patch.object(Path, "rename", autospec=True, side_effect=peer_recreates_then_third_creates)
    with pytest.raises(Timeout):
        SoftFileLock(stale_lock).acquire(blocking=False)

    # link never replaces, so the third marker keeps the path and the live one waits under the break name.
    assert (
        stale_lock.read_text(encoding="utf-8"),
        [path.read_text(encoding="utf-8") for path in stale_lock.parent.glob("test.lock.break.*")],
    ) == ("third", ["live"])


@NEEDS_SYMLINK
@pytest.mark.usefixtures("break_route")
def test_break_lock_file_never_links_a_symlink_swapped_in_before_the_take(  # pragma: needs symlink
    stale_lock: Path, mocker: MockerFixture, on_break_recheck: Callable[[Path, Callable[[], None]], None]
) -> None:
    (target := stale_lock.with_name("target")).write_text("victim", encoding="utf-8")

    def peer_swaps_in_a_symlink() -> None:
        stale_lock.unlink()
        stale_lock.symlink_to(target)

    on_break_recheck(stale_lock, peer_swaps_in_a_symlink)
    with pytest.raises(Timeout):
        SoftFileLock(stale_lock).acquire(blocking=False)
    mocker.stopall()

    # Linking a symlink back with link's default would publish the target's inode at the lock path.
    assert (target.stat().st_nlink, target.read_text(encoding="utf-8")) == (1, "victim")


@NEEDS_SYMLINK
def test_break_lock_file_never_links_a_symlink_swapped_in_under_the_break_name(  # pragma: needs symlink
    stale_lock: Path, mocker: MockerFixture
) -> None:
    (target := stale_lock.with_name("target")).write_text("private", encoding="utf-8")
    lstat: Final = os.lstat

    def peer_recreates_then_swaps_in_a_symlink(path: str, *, dir_fd: int | None = None) -> os.stat_result:
        if ".break." not in os.fspath(path):
            return lstat(path, dir_fd=dir_fd)
        _rewrite_in_place(Path(path))
        found: Final = lstat(path, dir_fd=dir_fd)
        Path(path).unlink()
        Path(path).symlink_to(target)
        return found

    mocker.patch("os.lstat", side_effect=peer_recreates_then_swaps_in_a_symlink)
    with pytest.raises(Timeout):
        SoftFileLock(stale_lock).acquire(blocking=False)
    mocker.stopall()

    assert target.stat().st_nlink == 1


@pytest.mark.skipif(sys.platform == "win32", reason="the liveness probe of a Windows PID does not call os.kill")
@pytest.mark.parametrize(
    ("lock_type", "record"),
    [
        pytest.param(SoftFileLock, _DEAD_HOLDER, id="soft"),
        pytest.param(MarkerSoftFileLock, _DEAD_OWNER, id="marker"),
        pytest.param(SoftFileLease, _DEAD_OWNER, id="lease"),
    ],
)
def test_stale_break_spares_a_successor_on_the_freed_inode_number(  # pragma: win32 no cover
    tmp_path: Path,
    mocker: MockerFixture,
    reuse_inode: Callable[[Path, os.stat_result], None],
    lock_type: type[SoftFileLock],
    record: str,
) -> None:
    (lock := tmp_path / "test.lock").write_text(record, encoding="utf-8")
    stale: Final = lock.lstat()

    def owner_gone_and_successor_acquired(pid: int, signal: int) -> None:
        # Between the stale read and the break the marker goes and the next holder's marker takes the freed inode
        # number, created in the same timestamp tick.
        lock.unlink()
        lock.write_text("live", encoding="utf-8")
        os.utime(lock, ns=(stale.st_atime_ns, stale.st_mtime_ns))
        reuse_inode(lock, stale)
        raise ProcessLookupError(ESRCH, f"no process {pid} for signal {signal}")

    mocker.patch("filelock._identity.os.kill", side_effect=owner_gone_and_successor_acquired)
    with pytest.raises(Timeout):
        lock_type(lock).acquire(blocking=False)
    mocker.stopall()

    assert lock.read_text(encoding="utf-8") == "live"


_STALE_BREAKERS: Final[pytest.MarkDecorator] = pytest.mark.parametrize(
    ("lock_type", "record"),
    [
        pytest.param(SoftFileLock, _DEAD_HOLDER, id="soft"),
        pytest.param(MarkerSoftFileLock, _DEAD_OWNER, id="marker"),
        pytest.param(SoftFileLease, _DEAD_OWNER, id="lease"),
    ],
)


@pytest.mark.skipif(sys.platform == "win32", reason="Windows cannot rename a file held open, so it pins nothing")
@_STALE_BREAKERS
def test_stale_break_skips_a_marker_swapped_in_after_the_pin(  # pragma: win32 no cover
    tmp_path: Path, mocker: MockerFixture, lock_type: type[SoftFileLock], record: str
) -> None:
    (lock := tmp_path / "test.lock").write_text(record, encoding="utf-8")
    fstat: Final = os.fstat
    swapped: list[bool] = []

    def pin_then_peer_swaps_the_marker(fd: int) -> os.stat_result:
        found: Final = fstat(fd)
        if not swapped:
            swapped.append(True)
            (replacement := lock.with_name("replacement")).write_text(record, encoding="utf-8")
            replacement.replace(lock)
        return found

    mocker.patch("os.fstat", side_effect=pin_then_peer_swaps_the_marker)
    rename: Final = mocker.spy(Path, "rename")
    with pytest.raises(Timeout):
        lock_type(lock).acquire(blocking=False)
    mocker.stopall()

    # Nothing keeps the swapped-in node's inode allocated, so its number proves nothing by the time of a break.
    assert (lock.read_text(encoding="utf-8"), rename.call_count) == (record, 0)


@pytest.mark.skipif(sys.platform == "win32", reason="Windows cannot rename a file held open, so it pins nothing")
@_STALE_BREAKERS
def test_stale_break_reclaims_a_marker_it_cannot_pin(  # pragma: win32 no cover
    tmp_path: Path, mocker: MockerFixture, lock_type: type[SoftFileLock], record: str
) -> None:
    # Without O_PATH a symlink or a socket cannot be opened, and the check falls back to the number it read.
    (lock := tmp_path / "test.lock").write_text(record, encoding="utf-8")
    open_: Final = os.open
    pinned: list[bool] = []

    def pin_refused(path: str, flags: int, mode: int = 0o777, *, dir_fd: int | None = None) -> int:
        # The pin is the first open of the existing marker; the read after it opens the file for real.
        if os.fspath(path) == os.fspath(lock) and not flags & os.O_CREAT and not pinned:
            pinned.append(True)
            raise OSError(ELOOP, "Too many levels of symbolic links", os.fspath(path))
        return open_(path, flags, mode, dir_fd=dir_fd)

    mocker.patch("os.open", side_effect=pin_refused)
    with lock_type(lock, timeout=1) as held:
        assert held.is_locked


@_STALE_BREAKERS
def test_stale_break_lets_acquire_retry_after_the_marker_vanishes_before_the_pin(
    tmp_path: Path, mocker: MockerFixture, lock_type: type[SoftFileLock], record: str
) -> None:
    (lock := tmp_path / "test.lock").write_text(record, encoding="utf-8")
    open_: Final = os.open
    released: list[bool] = []

    def holder_releases_then_open(path: str, flags: int, mode: int = 0o777, *, dir_fd: int | None = None) -> int:
        if os.fspath(path) == os.fspath(lock) and not flags & os.O_CREAT and not released:
            released.append(True)
            lock.unlink()
        return open_(path, flags, mode, dir_fd=dir_fd)

    mocker.patch("os.open", side_effect=holder_releases_then_open)
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
def test_break_lock_file_spares_a_file_that_replaced_what_it_swapped_in(  # pragma: win32 no cover
    stale_lock: Path, mocker: MockerFixture, lifetime: float | None
) -> None:
    os.utime(stale_lock, (0, 0))
    lstat: Final = os.lstat
    replaced: list[bool] = []

    def peer_replaces_the_swapped_in_file(path: str | os.PathLike[str], *, dir_fd: int | None = None) -> os.stat_result:
        found: Final = lstat(path, dir_fd=dir_fd)
        if ".break." in os.fspath(path) and not replaced:
            replaced.append(True)
            (other := stale_lock.with_name("other")).write_text("other", encoding="utf-8")
            other.replace(stale_lock)
        return found

    mocker.patch.object(os, "lstat", new=peer_replaces_the_swapped_in_file)
    with pytest.raises(Timeout):
        SoftFileLock(stale_lock, lifetime=lifetime).acquire(blocking=False)
    mocker.stopall()

    assert stale_lock.read_text(encoding="utf-8") == "other"


@pytest.mark.skipif(sys.platform == "win32", reason="Windows cannot exchange two names")
@_STALE_BREAKERS
def test_stale_break_takes_the_lock_over_in_one_poll(  # pragma: win32 no cover
    tmp_path: Path, lock_type: type[SoftFileLock], record: str
) -> None:
    (lock := tmp_path / "test.lock").write_text(record, encoding="utf-8")
    contender: Final = lock_type(lock)
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
    stale: Final = stale_lock.lstat()

    def successor_takes_the_freed_number() -> None:
        # A peer broke the stale marker after the recheck and the next holder's marker got its inode number, created in
        # the same timestamp tick, so only the pinned inode tells the two apart once the exchange has taken it.
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
    mocker.patch("os.link", side_effect=PermissionError(1, "Operation not permitted"))
    on_break_recheck(stale_lock, lambda: _rewrite_in_place(stale_lock))
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
    # NFS and SMB refuse the exchange; renaming the marker aside instead would empty the path beside a live holder.
    renamer: Final = mocker.MagicMock(return_value=error)
    mocker.patch("filelock._util._rename_call", return_value=(renamer, 2))
    with pytest.raises(Timeout):
        SoftFileLock(stale_lock).acquire(blocking=False)

    assert (
        stale_lock.read_text(encoding="utf-8"),
        list(stale_lock.parent.glob("test.lock.break.*")),
        renamer.called,
    ) == (
        _DEAD_HOLDER,
        [],
        True,
    )


@pytest.mark.skipif(sys.platform == "win32", reason="Windows cannot exchange two names")
def test_stale_take_over_interrupted_before_the_check_puts_the_marker_back(  # pragma: win32 no cover
    stale_lock: Path, mocker: MockerFixture
) -> None:
    # Between the exchange and the check the lock path names the contender's own marker; an interrupt there must not
    # leave it, naming a live PID that holds nothing, nor keep the marker it took.
    lstat: Final = os.lstat
    interrupted: list[bool] = []

    def interrupt_after_the_exchange(path: str | os.PathLike[str], *, dir_fd: int | None = None) -> os.stat_result:
        if ".break." in os.fspath(path) and not interrupted:
            interrupted.append(True)
            raise KeyboardInterrupt
        return lstat(path, dir_fd=dir_fd)

    mocker.patch.object(os, "lstat", new=interrupt_after_the_exchange)
    lock: Final = SoftFileLock(stale_lock)
    with pytest.raises(KeyboardInterrupt):
        lock.acquire(blocking=False)
    mocker.stopall()

    assert (
        stale_lock.read_text(encoding="utf-8"),
        list(stale_lock.parent.glob("test.lock.break.*")),
        lock.is_locked,
    ) == (
        _DEAD_HOLDER,
        [],
        False,
    )


def test_break_lock_file_break_name_is_unguessable(stale_lock: Path, mocker: MockerFixture) -> None:
    # A second breaker in this process could compute <lock>.break.<pid> and rename a recreated live lock onto it
    # between our lstat and unlink. Were that our break name, our unlink would delete the live lock.
    guessable: Final[Path] = stale_lock.with_name(f"test.lock.break.{os.getpid()}")
    lstat: Final = os.lstat

    def peer_moves_a_live_lock_onto_the_guessable_name(path: str | os.PathLike[str]) -> os.stat_result:
        result: Final[os.stat_result] = lstat(path)
        if ".break." in os.fspath(path) and not guessable.exists():
            stale_lock.write_text("live", encoding="utf-8")
            stale_lock.rename(guessable)
        return result

    mocker.patch("os.lstat", side_effect=peer_moves_a_live_lock_onto_the_guessable_name)
    with pytest.raises(Timeout):
        SoftFileLock(stale_lock).acquire(blocking=False)

    assert guessable.read_text(encoding="utf-8") == "live"


@pytest.mark.skipif(sys.platform == "win32", reason="symlink-to-dir raises IsADirectoryError only on Unix")
def test_writability_check_does_not_follow_symlink_to_dir(tmp_path: Path) -> None:  # pragma: win32 no cover
    (target := tmp_path / "targetdir").mkdir()
    (link := tmp_path / "my.lock").symlink_to(target)
    # Following the symlink would see a directory and raise IsADirectoryError; the lock waits on the link instead.
    with pytest.raises(Timeout):
        SoftFileLock(link).acquire(blocking=False)


@pytest.mark.skipif(sys.platform == "win32", reason="symlink + 0o444 semantics differ on Windows")
@_SKIP_AS_ROOT  # pragma: win32 no cover
def test_writability_check_does_not_follow_symlink_to_readonly(tmp_path: Path) -> None:
    (target := tmp_path / "readonly").write_text("x", encoding="utf-8")
    target.chmod(0o444)
    (link := tmp_path / "my.lock").symlink_to(target)
    # Following the symlink would see a read-only file and raise PermissionError; the link itself is writable.
    with pytest.raises(Timeout):
        SoftFileLock(link).acquire(blocking=False)


@pytest.mark.skipif(sys.platform == "win32", reason="a real directory raises PermissionError on Windows")
@pytest.mark.parametrize(
    "mode",
    [
        pytest.param(0o755, id="writable"),
        pytest.param(0o555, id="readonly", marks=NEEDS_FILE_PERMISSIONS),
    ],
)
@pytest.mark.parametrize("mtime", [pytest.param(0, id="mtime-zero"), pytest.param(2_000_000_000, id="mtime-future")])
def test_writability_check_rejects_a_directory(tmp_path: Path, mode: int, mtime: int) -> None:  # pragma: win32 no cover
    (path := tmp_path / "a_dir").mkdir()
    os.utime(path, (mtime, mtime))
    path.chmod(mode)
    try:
        with pytest.raises(IsADirectoryError):
            SoftFileLock(path).acquire(blocking=False)
    finally:
        path.chmod(0o755)


@_SKIP_AS_ROOT
@pytest.mark.parametrize("mtime", [pytest.param(0, id="mtime-zero"), pytest.param(2_000_000_000, id="mtime-future")])
def test_writability_check_rejects_a_readonly_file(tmp_path: Path, mtime: int) -> None:
    (path := tmp_path / "ro.lock").write_text("x", encoding="utf-8")
    path.chmod(0o444)
    try:
        os.utime(path, (mtime, mtime))
        with pytest.raises(PermissionError):
            SoftFileLock(path).acquire(blocking=False)
    finally:
        path.chmod(0o644)


@pytest.mark.parametrize(
    "mode", [pytest.param(0o444, id="acl"), pytest.param(0o464, id="group"), pytest.param(0o446, id="other")]
)
@pytest.mark.parametrize("effective_ids", [pytest.param(True, id="supported"), pytest.param(False, id="unsupported")])
def test_writability_uses_open_credentials(
    readonly_marker: Path, mocker: MockerFixture, mode: int, *, effective_ids: bool
) -> None:
    readonly_marker.chmod(mode)

    def access(filename: str, permission: int, *, effective_ids: bool = False) -> bool:
        assert (filename, permission) == (str(readonly_marker), os.W_OK)
        return effective_ids == supported

    supported: Final = effective_ids
    probe: Final = mocker.patch("os.access", autospec=True, side_effect=access)
    mocker.patch("os.supports_effective_ids", {probe} if effective_ids else set())
    with pytest.raises(Timeout):
        SoftFileLock(readonly_marker).acquire(timeout=0)


def test_writability_allows_creation_after_marker_disappears(readonly_marker: Path, mocker: MockerFixture) -> None:
    def access(filename: str, permission: int, *, effective_ids: bool = False) -> bool:
        assert (permission, effective_ids) == (os.W_OK, os.access in os.supports_effective_ids)
        readonly_marker.chmod(0o644)
        Path(filename).unlink()
        return False

    mocker.patch("os.access", autospec=True, side_effect=access)
    with (lock := SoftFileLock(readonly_marker)).acquire(timeout=0):
        assert lock.is_locked


def test_writability_rejects_denied_marker(readonly_marker: Path, mocker: MockerFixture) -> None:
    mocker.patch("os.access", autospec=True, return_value=False)
    with pytest.raises(PermissionError):
        SoftFileLock(readonly_marker).acquire(timeout=0)


@pytest.fixture
def readonly_marker(tmp_path: Path) -> Generator[Path]:
    path: Final = tmp_path / "test.lock"
    path.write_text(f"424242\n{host_name()}-other\n", encoding="utf-8")
    path.chmod(0o444)
    try:
        yield path
    finally:
        with suppress(FileNotFoundError):
            path.chmod(0o644)


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        pytest.param(None, False, id="privileged"),
        pytest.param(PermissionError(EACCES, "denied"), True, id="access-denied"),
        pytest.param(PermissionError(EPERM, "denied"), True, id="operation-denied"),
    ],
)
@pytest.mark.usefixtures("_read_failure")
def test_file_permissions_capability(*, expected: bool) -> None:
    assert runpy.run_module("capabilities")["CAPABILITIES"]["file-permissions"] is expected


@pytest.mark.parametrize(
    "error",
    [
        pytest.param(OSError(EIO, "probe failed"), id="io-error"),
        pytest.param(OSError(ENOENT, "probe failed"), id="missing-file"),
    ],
)
@pytest.mark.usefixtures("_read_failure")
def test_file_permissions_probe_propagates_io_errors(error: OSError) -> None:
    with pytest.raises(OSError, match="probe failed") as raised:
        runpy.run_module("capabilities")
    assert raised.value is error


@pytest.fixture
def _read_failure(mocker: MockerFixture, error: OSError | None) -> None:
    mocker.patch.object(Path, "read_bytes", autospec=True, side_effect=error, return_value=b"")
