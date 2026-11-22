from __future__ import annotations

import os
import subprocess  # ruff:ignore[suspicious-subprocess-import]  # only fixed-argument, no-shell runs below
import sys
import threading
import time
from contextlib import closing
from errno import EWOULDBLOCK
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Final

if sys.platform == "win32":  # pragma: win32 cover
    import ctypes
import pytest

pytest.importorskip("sqlite3")
import sqlite3

from filelock import ReadWriteLock, Timeout
from tests.capability_marks import SKIP_ON_UNRELIABLE_PROCESS_SYNC

if sys.platform == "win32":  # pragma: win32 cover
    from filelock._filesystem import directory_is_private, make_private_directory
    from filelock._filesystem._windows._directories import ADVAPI32
    from filelock._identity import host_name
    from filelock._sqlite._links import _host_digest, _remove_link_directory_on_windows

if TYPE_CHECKING:
    from collections.abc import Callable

    from pytest_mock import MockerFixture


pytestmark: Final[pytest.MarkDecorator] = SKIP_ON_UNRELIABLE_PROCESS_SYNC


@pytest.mark.skipif(sys.platform != "win32", reason="Windows only")  # pragma: win32 cover
def test_windows_read_write_rejects_changed_database_link(tmp_path: Path, mocker: MockerFixture) -> None:
    database = tmp_path / "database.db"
    mocker.patch(
        "filelock._sqlite._connection.directory_is_private",
        side_effect=lambda path: path != os.fspath(tmp_path),
        autospec=True,
    )
    mocker.patch("filelock._sqlite._links.identity_at", return_value=(-1, -1), autospec=True)
    with pytest.raises(OSError, match="lock database changed"):
        ReadWriteLock(database, is_singleton=False)
    assert [entry.name for entry in tmp_path.iterdir()] == [database.name]


@pytest.mark.skipif(sys.platform != "win32", reason="Windows directory handles enforce rename sharing")
def test_windows_read_write_keeps_private_link_pinned_until_sqlite_opens(  # pragma: win32 cover
    everyone_writable_database: Path, tmp_path: Path, mocker: MockerFixture
) -> None:
    connect = sqlite3.connect
    opened: list[Path] = []

    def connect_while_pinned(
        database: str,
        *,
        check_same_thread: bool,
        factory: type[sqlite3.Connection],
        cached_statements: int,
        timeout: float,
    ) -> sqlite3.Connection:
        directory = Path(database).parent.parent
        with pytest.raises(PermissionError) as error:
            directory.rename(tmp_path / "moved")
        assert getattr(error.value, "winerror", None) == 32
        opened.append(directory)
        return connect(
            database,
            check_same_thread=check_same_thread,
            factory=factory,
            cached_statements=cached_statements,
            timeout=timeout,
        )

    mocker.patch.object(sqlite3, "connect", side_effect=connect_while_pinned, autospec=True)
    with ReadWriteLock(everyone_writable_database, is_singleton=False).write_lock(), pytest.raises(PermissionError):
        opened[-1].rename(tmp_path / "moved")

    assert opened
    assert not opened[-1].exists()


@pytest.mark.skipif(sys.platform != "win32", reason="share modes exist only on Windows")
def test_read_write_lock_refuses_a_database_held_without_sharing(tmp_path: Path) -> None:  # pragma: win32 cover
    assert sys.platform == "win32"
    # Another process holding the database with no sharing blocks the handle that pins its name, so construction fails.
    lock_path = tmp_path / "lock.db"  # pragma: win32 cover
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # pragma: win32 cover
    kernel32.CreateFileW.restype = ctypes.c_void_p  # pragma: win32 cover
    # GENERIC_READ | GENERIC_WRITE, no sharing, CREATE_ALWAYS, FILE_ATTRIBUTE_NORMAL
    handle = kernel32.CreateFileW(str(lock_path), 0xC0000000, 0, None, 2, 0x80, None)  # pragma: win32 cover
    try:  # pragma: win32 cover
        with pytest.raises(PermissionError, match="held without sharing"):
            ReadWriteLock(lock_path, is_singleton=False)
    finally:  # pragma: win32 cover
        kernel32.CloseHandle(ctypes.c_void_p(handle))


@pytest.mark.skipif(sys.platform != "win32", reason="oplocks exist only on Windows")
def test_read_write_lock_oplocked_database_raises_timeout_after_its_timeout(
    tmp_path: Path, hold_oplock: Callable[[Path], Callable[[], None]]
) -> None:  # pragma: win32 cover
    with closing(ReadWriteLock(database := tmp_path / "lock.db", is_singleton=False)) as lock:
        hold_oplock(database)

        started = time.monotonic()
        with pytest.raises(Timeout):
            lock.acquire_read(timeout=1)
        assert 0.9 <= time.monotonic() - started < 5


@pytest.mark.skipif(sys.platform != "win32", reason="oplocks exist only on Windows")
def test_read_write_lock_waits_for_an_oplock_holder_that_answers(
    tmp_path: Path, hold_oplock: Callable[[Path], Callable[[], None]]
) -> None:  # pragma: win32 cover
    with closing(ReadWriteLock(database := tmp_path / "lock.db", is_singleton=False)):
        pass
    timer = threading.Timer(1.0, hold_oplock(database))
    started = time.monotonic()
    timer.start()
    try:
        with closing(ReadWriteLock(database, is_singleton=False)) as lock, lock.read_lock():
            assert time.monotonic() - started >= 1
    finally:
        timer.join()


def test_read_write_lock_open_blocked_by_an_oplock_is_a_timeout(tmp_path: Path, mocker: MockerFixture) -> None:
    with closing(ReadWriteLock(tmp_path / "lock.db", is_singleton=False)) as lock:
        mocker.patch(
            "filelock._sqlite._transactions.connect",
            side_effect=BlockingIOError(EWOULDBLOCK, "oplock break"),
            autospec=True,
        )

        with pytest.raises(Timeout):
            lock.acquire_read(timeout=0)


@pytest.mark.skipif(sys.platform != "win32", reason="POSIX creates the database 0o600 through its mode bits")
def test_read_write_lock_database_keeps_out_the_directory_acl(
    inheriting_directory: Path,
) -> None:  # pragma: win32 cover
    lock_path = inheriting_directory / "lock.db"
    with closing(ReadWriteLock(lock_path, is_singleton=False)):
        acl = subprocess.run(
            [Path(os.environ["SYSTEMROOT"], "System32", "icacls.exe"), lock_path], capture_output=True, check=True
        ).stdout
    assert b"(I)" not in acl  # icacls marks inherited entries with (I) in every display language


@pytest.mark.skipif(sys.platform != "win32", reason="a Windows directory is shared through its ACL")
def test_windows_directory_whose_acl_cannot_be_read_gets_a_private_link(  # pragma: win32 cover
    tmp_path: Path, mocker: MockerFixture
) -> None:
    # An account without read access to a directory's ACL, as with C:\Windows\Temp, cannot tell who may write there.
    read_acl = ADVAPI32.GetNamedSecurityInfoW
    denials = [5]
    mocker.patch.object(
        ADVAPI32,
        "GetNamedSecurityInfoW",
        side_effect=lambda *args: denials.pop() if denials else read_acl(*args),
        autospec=True,
    )
    connect = mocker.spy(sqlite3, "connect")

    ReadWriteLock(tmp_path / "lock.db", is_singleton=False).close()

    opened = Path(connect.call_args_list[0].args[0])
    assert (opened.name, opened.parent.parent.parent) == ("lock.db", tmp_path)


@pytest.mark.skipif(sys.platform != "win32", reason="a Windows directory is shared through its ACL")
@pytest.mark.parametrize("right", [pytest.param("WDAC", id="write-dac"), pytest.param("WO", id="write-owner")])
def test_windows_directory_whose_acl_can_be_rewritten_gets_a_private_link(  # pragma: win32 cover
    tmp_path: Path, mocker: MockerFixture, right: str
) -> None:
    # An account that can rewrite a directory's ACL or take its ownership can grant itself the right to add names.
    directory = tmp_path / "rewritable"
    directory.mkdir()
    icacls = Path(os.environ["SYSTEMROOT"], "System32", "icacls.exe")
    subprocess.run([icacls, directory, "/grant", f"*S-1-1-0:({right})"], capture_output=True, check=True)
    connect = mocker.spy(sqlite3, "connect")

    ReadWriteLock(directory / "lock.db", is_singleton=False).close()

    opened = Path(connect.call_args_list[0].args[0])
    assert (directory_is_private(os.fspath(directory)), opened.parent.parent.parent) == (False, directory)


@pytest.mark.skipif(sys.platform != "win32", reason="a Windows directory is shared through its ACL")
def test_windows_directory_only_the_account_can_write_connects_by_name(  # pragma: win32 cover
    tmp_path: Path, mocker: MockerFixture
) -> None:
    connect = mocker.spy(sqlite3, "connect")
    database = tmp_path / "lock.db"

    ReadWriteLock(database, is_singleton=False).close()

    assert (directory_is_private(os.fspath(tmp_path)), connect.call_args_list[0].args[0]) == (True, str(database))


@pytest.mark.skipif(sys.platform != "win32", reason="a Windows directory is shared through its ACL")
def test_windows_shared_directory_leaves_only_the_database_after_release(  # pragma: win32 cover
    everyone_writable_database: Path,
) -> None:
    lock = ReadWriteLock(everyone_writable_database, is_singleton=False)
    lock.acquire_read()
    assert len(list(everyone_writable_database.parent.iterdir())) == 2
    lock.release()

    assert list(everyone_writable_database.parent.iterdir()) == [everyone_writable_database]


@pytest.mark.skipif(sys.platform != "win32", reason="a Windows directory is shared through its ACL")
@pytest.mark.parametrize(
    "boundary", [pytest.param("os.link", id="link"), pytest.param("sqlite3.connect", id="connect")]
)
def test_windows_shared_directory_removes_the_link_after_a_failure(  # pragma: win32 cover
    everyone_writable_database: Path, mocker: MockerFixture, boundary: str
) -> None:
    mocker.patch(boundary, autospec=True, side_effect=OSError("cannot open database"))

    with pytest.raises(OSError, match="cannot open database"):
        ReadWriteLock(everyone_writable_database, is_singleton=False)

    assert list(everyone_writable_database.parent.iterdir()) == [everyone_writable_database]


@pytest.mark.skipif(sys.platform != "win32", reason="a Windows directory is shared through its ACL")
def test_windows_shared_directory_refuses_a_link_directory_others_can_write(  # pragma: win32 cover
    everyone_writable_database: Path, mocker: MockerFixture
) -> None:
    mocker.patch("filelock._sqlite._links.directory_is_private", autospec=True, return_value=False)

    with pytest.raises(OSError, match="replaced while opening"):
        ReadWriteLock(everyone_writable_database, is_singleton=False)

    assert list(everyone_writable_database.parent.iterdir()) == [everyone_writable_database]


@pytest.mark.skipif(sys.platform != "win32", reason="a Windows directory is shared through its ACL")
def test_windows_shared_directory_release_keeps_a_directory_renamed_onto_the_link(  # pragma: win32 cover
    everyone_writable_database: Path, tmp_path: Path, mocker: MockerFixture
) -> None:
    other = tmp_path / "other"
    other.mkdir()
    (other / "notes.txt").write_text("keep")
    renamed: list[Path] = []

    def replace_directory(path: Path, remove: Callable[[], None]) -> None:
        path.rename(tmp_path / "aside")
        other.rename(path)
        renamed.append(path)
        remove()

    mocker.patch(
        "filelock._sqlite._links._remove_link_directory_on_windows",
        autospec=True,
        side_effect=lambda link: replace_directory(link.path, partial(_remove_link_directory_on_windows, link)),
    )
    ReadWriteLock(everyone_writable_database, is_singleton=False).close()

    assert (renamed[0] / "notes.txt").read_text() == "keep"


@pytest.mark.skipif(sys.platform != "win32", reason="a Windows directory is shared through its ACL")
def test_windows_shared_directory_sweeps_the_link_of_a_dead_holder(  # pragma: win32 cover
    everyone_writable_database: Path,
) -> None:
    everyone_writable_database.touch()
    left = everyone_writable_database.parent / f".filelock-{2**31 - 1}--{_host_digest(host_name())}-x"
    make_private_directory(os.fspath(left))
    (left / "0123456789abcdef").mkdir()
    (left / "0123456789abcdef" / "lock.db").hardlink_to(everyone_writable_database)

    with ReadWriteLock(everyone_writable_database, is_singleton=False).read_lock():
        pass

    assert (_link_directories(everyone_writable_database), everyone_writable_database.stat().st_nlink) == ([], 1)


def _link_directories(database: Path) -> list[Path]:  # pragma: win32 cover
    return sorted(path for path in database.parent.iterdir() if path.name.startswith(".filelock-"))


@pytest.mark.skipif(sys.platform != "win32", reason="a Windows directory is shared through its ACL")
def test_windows_shared_directory_sweep_keeps_a_file_that_is_not_a_link_to_the_database(  # pragma: win32 cover
    everyone_writable_database: Path,
) -> None:
    left = everyone_writable_database.parent / f".filelock-{2**31 - 1}--{_host_digest(host_name())}-x"
    make_private_directory(os.fspath(left))
    (left / "0123456789abcdef").mkdir()
    (left / "0123456789abcdef" / "lock.db").write_text("keep")

    ReadWriteLock(everyone_writable_database, is_singleton=False).close()

    assert (left / "0123456789abcdef" / "lock.db").read_text() == "keep"


@pytest.mark.skipif(sys.platform != "win32", reason="a Windows directory is shared through its ACL")
def test_windows_shared_directory_sweep_keeps_the_link_of_a_live_holder(  # pragma: win32 cover
    everyone_writable_database: Path, windows_link_holder: subprocess.Popen[str]
) -> None:
    left = _link_directories(everyone_writable_database)

    ReadWriteLock(everyone_writable_database, is_singleton=False).close()

    assert (len(left), _link_directories(everyone_writable_database)) == (1, left)
    assert windows_link_holder.poll() is None
