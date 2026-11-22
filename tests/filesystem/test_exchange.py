from __future__ import annotations

import os
import re
import sys
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING, Final

import pytest

import filelock._filesystem._exchange  # simplint: ignore[private-import-path]  # wrap the Windows rename pin
from filelock import SoftFileLock, Timeout
from filelock._filesystem import (
    break_lock_file,
    break_marker_path,
    break_marker_pid,
    exchange_names,
    write_break_marker,
)
from tests.capability_marks import NEEDS_STALE_RECLAIM, NEEDS_SYMLINK, NEEDS_UNLINK_OPEN_FILE
from tests.conftest import DEAD_HOLDER
from tests.filesystem.conftest import replace_with_same_mtime, rewrite_in_place
from tests.process_helpers import call_within

if TYPE_CHECKING:
    from collections.abc import Callable
    from contextlib import AbstractContextManager

    from pytest_mock import MockerFixture


def test_break_marker_format(tmp_path: Path) -> None:
    name = Path(break_marker_path(str(tmp_path / "test.lock"), os.getpid()))
    assert re.fullmatch(rf"test\.lock\.break\.{os.getpid()}\.[0-9a-f]{{32}}", name.name)
    with name.open("xb") as marker:
        write_break_marker(marker.fileno())
    assert name.read_bytes() == f"filelock/break\n{os.getpid()}\n".encode("ascii")


@pytest.mark.parametrize(
    ("record", "expected"),
    [
        pytest.param("filelock/break\n007\n", 7, id="leading-zeros"),
        pytest.param("filelock/break\n\uff10\uff17\n", None, id="non-ascii-pid"),
        pytest.param("filelock/break\n+7\n", None, id="signed-pid"),
        pytest.param("filelock/break\n7", None, id="torn-record"),
        pytest.param("filelock/break\n7\n\n", None, id="extra-line"),
    ],
)
def test_break_marker_pid(record: str, expected: int | None) -> None:
    assert break_marker_pid(record) == expected


@NEEDS_STALE_RECLAIM
def test_break_lock_file_unlinks_a_dead_holders_marker(stale_lock: Path) -> None:
    with SoftFileLock(stale_lock, timeout=1):
        assert list(stale_lock.parent.glob("test.lock.break.*")) == []


@pytest.mark.skipif(sys.platform == "win32", reason="Windows can reclaim a pinned marker without exchange")
@pytest.mark.usefixtures("break_route")
@pytest.mark.parametrize("break_route", ["no-exchange"], indirect=True)
def test_stale_marker_is_retained_without_exchange(stale_lock: Path) -> None:  # pragma: win32 no cover
    with pytest.raises(Timeout):
        SoftFileLock(stale_lock).acquire(blocking=False)
    assert (stale_lock.read_text(encoding="utf-8"), list(stale_lock.parent.glob("test.lock.break.*"))) == (
        DEAD_HOLDER,
        [],
    )


@pytest.mark.skipif(sys.platform != "win32", reason="oplocks exist only on Windows")
def test_break_lock_file_skips_a_marker_under_an_oplock(  # pragma: win32 cover
    stale_lock: Path, hold_oplock: Callable[[Path], Callable[[], None]]
) -> None:
    held = stale_lock.stat()
    hold_oplock(stale_lock)

    finished, error = call_within(lambda: break_lock_file(str(stale_lock), held.st_mtime, held.st_ino), 5)

    assert (finished, type(error), stale_lock.exists()) == (True, BlockingIOError, True)


@pytest.mark.usefixtures("break_route")
@pytest.mark.parametrize(
    "recreate",
    [pytest.param(rewrite_in_place, id="mtime-advanced"), pytest.param(replace_with_same_mtime, id="inode-changed")],
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


@pytest.mark.skipif(sys.platform == "win32", reason="Windows cannot exchange two names")
@NEEDS_STALE_RECLAIM
def test_break_lock_file_restores_a_peer_marker_taken_by_exchange(  # pragma: win32 no cover
    stale_lock: Path, mocker: MockerFixture
) -> None:
    stale = stale_lock.stat()
    exchange = exchange_names
    replaced: list[bool] = []

    def replace_before_exchange(source: str, target: str) -> bool:
        if not replaced:
            replaced.append(True)
            stale_lock.unlink()
            stale_lock.write_text("live", encoding="utf-8")
        return exchange(source, target)

    mocker.patch("filelock._filesystem._exchange.exchange_names", side_effect=replace_before_exchange, autospec=True)
    break_lock_file(str(stale_lock), stale.st_mtime, stale.st_ino)

    assert (stale_lock.read_text(encoding="utf-8"), list(stale_lock.parent.glob("test.lock.break.*"))) == ("live", [])


@pytest.mark.parametrize(
    "recreate",
    [
        pytest.param(rewrite_in_place, id="mtime-advanced"),
        pytest.param(replace_with_same_mtime, id="inode-changed", marks=NEEDS_UNLINK_OPEN_FILE),
    ],
)
def test_break_lock_file_leaves_a_marker_recreated_since_the_stale_check(
    stale_lock: Path, mocker: MockerFixture, recreate: Callable[[Path], None]
) -> None:
    def read_then_peer_recreates(fd: int, length: int) -> bytes:
        data: Final[bytes] = real_read(fd, length)
        recreate(stale_lock)
        return data

    real_read = os.read
    mocker.patch("os.read", autospec=True, side_effect=read_then_peer_recreates)
    rename = mocker.spy(Path, "rename")
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
    # A third contender must see a marker throughout the break, even before the breaker verifies its owner.
    holder: Final[SoftFileLock] = SoftFileLock(stale_lock)
    overlaps: list[bool] = []
    polling: list[bool] = []
    real = getattr(os, point)

    def third_contender_polls(path: str | os.PathLike[str], *args: int, **kwargs: int | None) -> int | None:
        result = real(path, *args, **kwargs)
        if holder.is_locked and not polling and os.fspath(path).startswith(os.fspath(stale_lock)):
            polling.append(True)
            third: Final[SoftFileLock] = SoftFileLock(stale_lock)
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
    assert sys.platform == "win32"
    rename = Path.rename
    pin = filelock._filesystem._exchange._pinned_for_rename

    def peer_recreates_then_pin(
        path: str,
    ) -> AbstractContextManager[None]:
        rewrite_in_place(Path(path))  # Windows refuses an in-place rewrite once the pin holds the marker
        return pin(path)

    def rename_then_third_creates(source: Path, target: str) -> Path:
        result: Final[Path] = rename(source, target)
        source.write_text("third", encoding="utf-8")
        return result

    mocker.patch(
        "filelock._filesystem._exchange._pinned_for_rename", side_effect=peer_recreates_then_pin, autospec=True
    )
    mocker.patch.object(Path, "rename", autospec=True, side_effect=rename_then_third_creates)
    with pytest.raises(Timeout):
        SoftFileLock(stale_lock).acquire(blocking=False)

    # A third marker can win the path while the live marker remains under the break name.
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
    lstat = os.lstat

    def peer_recreates_then_swaps_in_a_symlink(path: str, *, dir_fd: int | None = None) -> os.stat_result:
        if ".break." not in os.fspath(path):
            return lstat(path, dir_fd=dir_fd)
        rewrite_in_place(Path(path))
        found: Final[os.stat_result] = lstat(path, dir_fd=dir_fd)
        Path(path).unlink()
        Path(path).symlink_to(target)
        return found

    mocker.patch("os.lstat", side_effect=peer_recreates_then_swaps_in_a_symlink, autospec=True)
    with pytest.raises(Timeout):
        SoftFileLock(stale_lock).acquire(blocking=False)
    mocker.stopall()

    assert target.stat().st_nlink == 1
