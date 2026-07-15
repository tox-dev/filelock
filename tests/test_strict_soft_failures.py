from __future__ import annotations

import os
import pickle  # noqa: S403  # round-trip uses bytes produced in this test
import socket
import stat
import sys
import time
from errno import EACCES, EEXIST, EIO, EXDEV
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if sys.version_info >= (3, 11):
    from builtins import BaseExceptionGroup
else:  # pragma: no cover (<py311)
    from exceptiongroup import BaseExceptionGroup

from filelock import SoftFileLock, SoftFileLockProtocolError, StrictSoftFileLock, Timeout

if TYPE_CHECKING:
    from collections.abc import Iterator

    from pytest_mock import MockerFixture

_PathValue = str | bytes | os.PathLike[str] | os.PathLike[bytes]


def test_strict_soft_protocol_error_pickles() -> None:
    error = SoftFileLockProtocolError("resource.lock", "held-v2-claim", "unknown version")

    restored = pickle.loads(pickle.dumps(error))  # noqa: S301  # input comes from the preceding local value

    assert (type(restored), restored.lock_file, restored.claim_name, restored.reason, str(restored)) == (
        SoftFileLockProtocolError,
        "resource.lock",
        "held-v2-claim",
        "unknown version",
        "Invalid strict soft-lock state at resource.lock: claim 'held-v2-claim': unknown version",
    )


def test_strict_soft_claims_empty_before_first_acquire(tmp_path: Path) -> None:
    assert StrictSoftFileLock(tmp_path / "resource.lock").claims == ()


def test_strict_soft_intent_token_collision_does_not_claim_lock(tmp_path: Path, mocker: MockerFixture) -> None:
    lock_path = tmp_path / "resource.lock"
    _initialize_protocol(lock_path)

    def collide_intent(
        _source: _PathValue,
        destination: _PathValue,
        **_options: int | bool | None,
    ) -> None:
        if Path(os.fsdecode(destination)).name.startswith("intent-"):
            raise FileExistsError(EEXIST, "claim exists")

    mocker.patch("filelock._strict.os.link", side_effect=collide_intent)
    lock = StrictSoftFileLock(lock_path, timeout=0)

    with pytest.raises(Timeout):
        lock.acquire()
    assert (lock.claims, lock.is_locked) == ((), False)


def test_strict_soft_held_publication_failure_rolls_back_intent(tmp_path: Path, mocker: MockerFixture) -> None:
    lock_path = tmp_path / "resource.lock"
    _initialize_protocol(lock_path)
    real_link = os.link

    def fail_held(
        source: _PathValue,
        destination: _PathValue,
        *,
        src_dir_fd: int | None = None,
        dst_dir_fd: int | None = None,
        follow_symlinks: bool = True,
    ) -> None:
        if Path(os.fsdecode(destination)).name.startswith("held-"):
            raise OSError(EXDEV, "hard links unavailable")
        real_link(
            source,
            destination,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
            follow_symlinks=follow_symlinks,
        )

    mocker.patch("filelock._strict.os.link", side_effect=fail_held)
    lock = StrictSoftFileLock(lock_path)

    with pytest.raises(SoftFileLockProtocolError, match="hard-link publication"):
        lock.acquire()
    assert (lock.claims, lock.is_locked) == ((), False)


def test_strict_soft_held_collision_does_not_delete_foreign_claim(tmp_path: Path, mocker: MockerFixture) -> None:
    lock_path = tmp_path / "resource.lock"
    _initialize_protocol(lock_path)
    real_link = os.link

    def publish_foreign_held(
        source: _PathValue,
        destination: _PathValue,
        *,
        src_dir_fd: int | None = None,
        dst_dir_fd: int | None = None,
        follow_symlinks: bool = True,
    ) -> None:
        real_link(
            source,
            destination,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
            follow_symlinks=follow_symlinks,
        )
        if Path(os.fsdecode(destination)).name.startswith("held-"):
            raise FileExistsError(EEXIST, "foreign claim won")

    mocker.patch("filelock._strict.os.link", side_effect=publish_foreign_held)

    with pytest.raises(FileExistsError, match="foreign claim won"):
        StrictSoftFileLock(lock_path).acquire()
    claims = StrictSoftFileLock(lock_path).claims
    assert (len(claims), claims[0].state) == (1, "held")


@pytest.mark.parametrize("scan_to_empty", [pytest.param(2, id="intent"), pytest.param(3, id="held")])
def test_strict_soft_force_break_during_doorway_backs_off(
    tmp_path: Path, mocker: MockerFixture, scan_to_empty: int
) -> None:
    lock_path = tmp_path / "resource.lock"
    _initialize_protocol(lock_path)
    real_scandir = os.scandir
    scans = 0

    def break_before_scan(path: str | os.PathLike[str]) -> Iterator[os.DirEntry[str]]:
        nonlocal scans
        scans += 1
        if scans == scan_to_empty:
            for claim in Path(os.fsdecode(path)).glob("*.claim"):
                claim.unlink()
        return real_scandir(path)

    mocker.patch("filelock._strict.os.scandir", side_effect=break_before_scan)
    lock = StrictSoftFileLock(lock_path, timeout=0)

    with pytest.raises(Timeout):
        lock.acquire()
    assert (lock.claims, lock.is_locked) == ((), False)


def test_strict_soft_force_break_held_during_doorway_backs_off(tmp_path: Path, mocker: MockerFixture) -> None:
    lock_path = tmp_path / "resource.lock"
    _initialize_protocol(lock_path)
    real_scandir = os.scandir
    scans = 0

    def break_held_before_final_scan(path: str | os.PathLike[str]) -> Iterator[os.DirEntry[str]]:
        nonlocal scans
        scans += 1
        if scans == 3:
            for claim in Path(os.fsdecode(path)).glob("held-*.claim"):
                claim.unlink()
        return real_scandir(path)

    mocker.patch("filelock._strict.os.scandir", side_effect=break_held_before_final_scan)
    lock = StrictSoftFileLock(lock_path, timeout=0)

    with pytest.raises(Timeout):
        lock.acquire()
    assert (lock.claims, lock.is_locked) == ((), False)


def test_strict_soft_reaper_removes_private_before_publication(tmp_path: Path, mocker: MockerFixture) -> None:
    lock_path = tmp_path / "resource.lock"
    _initialize_protocol(lock_path)
    token = "0" * 32
    private_path = _private_claim_path(lock_path, token)
    real_link = os.link

    def reap_before_link(
        source: _PathValue,
        destination: _PathValue,
        *,
        src_dir_fd: int | None = None,
        dst_dir_fd: int | None = None,
        follow_symlinks: bool = True,
    ) -> None:
        if Path(os.fsdecode(destination)).name.startswith("intent-"):
            os.utime(private_path, (0, 0))
            assert StrictSoftFileLock(lock_path).claims == ()
        real_link(
            source,
            destination,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
            follow_symlinks=follow_symlinks,
        )

    mocker.patch("filelock._strict.secrets.token_hex", return_value=token)
    mocker.patch("filelock._strict.os.link", side_effect=reap_before_link)
    lock = StrictSoftFileLock(lock_path, timeout=0)

    with pytest.raises(Timeout):
        lock.acquire()
    assert (lock.claims, lock.is_locked, private_path.exists()) == ((), False, False)


def test_strict_soft_reaper_removes_private_after_publication(tmp_path: Path, mocker: MockerFixture) -> None:
    lock_path = tmp_path / "resource.lock"
    _initialize_protocol(lock_path)
    token = "0" * 32
    private_path = _private_claim_path(lock_path, token)
    real_link = os.link

    def reap_after_link(
        source: _PathValue,
        destination: _PathValue,
        *,
        src_dir_fd: int | None = None,
        dst_dir_fd: int | None = None,
        follow_symlinks: bool = True,
    ) -> None:
        real_link(
            source,
            destination,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
            follow_symlinks=follow_symlinks,
        )
        if Path(os.fsdecode(destination)).name.startswith("intent-"):
            assert len(StrictSoftFileLock(lock_path).claims) == 1

    mocker.patch("filelock._strict.secrets.token_hex", return_value=token)
    mocker.patch("filelock._strict.os.link", side_effect=reap_after_link)

    with StrictSoftFileLock(lock_path, timeout=0) as lock:
        assert (lock.claims[0].state, private_path.exists()) == ("held", False)


def test_strict_soft_reaper_replacement_only_aborts_publisher(tmp_path: Path, mocker: MockerFixture) -> None:
    lock_path = tmp_path / "resource.lock"
    _initialize_protocol(lock_path)
    token = "0" * 32
    private_path = _private_claim_path(lock_path, token)
    displaced_path = private_path.with_suffix(".displaced")
    real_link = os.link
    real_unlink = Path.unlink
    replaced = False

    def replace_before_unlink(path: Path, *, missing_ok: bool = False) -> None:
        nonlocal replaced
        if path == private_path and not replaced:
            replaced = True
            path.replace(displaced_path)
            path.write_bytes(b"replacement publisher")
            return
        real_unlink(path, missing_ok=missing_ok)

    def reap_before_link(
        source: _PathValue,
        destination: _PathValue,
        *,
        src_dir_fd: int | None = None,
        dst_dir_fd: int | None = None,
        follow_symlinks: bool = True,
    ) -> None:
        if Path(os.fsdecode(destination)).name.startswith("intent-"):
            os.utime(private_path, (0, 0))
            assert StrictSoftFileLock(lock_path).claims == ()
        real_link(
            source,
            destination,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
            follow_symlinks=follow_symlinks,
        )

    mocker.patch("filelock._strict.secrets.token_hex", return_value=token)
    mocker.patch("filelock._strict.os.link", side_effect=reap_before_link)
    mocker.patch.object(Path, "unlink", autospec=True, side_effect=replace_before_unlink)
    lock = StrictSoftFileLock(lock_path, timeout=0)

    with pytest.raises(Timeout):
        lock.acquire()
    with pytest.raises(SoftFileLockProtocolError, match="malformed claim record"):
        _ = lock.claims
    public_path = private_path.parent / f"intent-v1-{token}.claim"
    assert (
        replaced,
        lock.is_locked,
        private_path.exists(),
        public_path.read_bytes(),
        displaced_path.exists(),
    ) == (
        True,
        False,
        False,
        b"replacement publisher",
        True,
    )


def test_strict_soft_private_close_failure_leaves_public_claim(tmp_path: Path, mocker: MockerFixture) -> None:
    lock_path = tmp_path / "resource.lock"
    _initialize_protocol(lock_path)
    real_close = os.close
    close_failed = False

    def fail_private_close(fd: int) -> None:
        nonlocal close_failed
        real_close(fd)
        if not close_failed:
            close_failed = True
            raise OSError(EIO, "private close failed")

    mocker.patch("filelock._strict.os.close", side_effect=fail_private_close)
    lock = StrictSoftFileLock(lock_path)

    with pytest.raises(OSError, match="private close failed"):
        lock.acquire()
    assert ([claim.state for claim in lock.claims], lock.is_locked) == (["intent"], False)


def test_strict_soft_link_and_directory_close_failures_preserve_both(tmp_path: Path, mocker: MockerFixture) -> None:
    lock_path = tmp_path / "resource.lock"
    _initialize_protocol(lock_path)
    real_close = os.close
    real_fstat = os.fstat
    directory_close_failed = False

    def fail_directory_close(fd: int) -> None:
        nonlocal directory_close_failed
        is_directory = stat.S_ISDIR(real_fstat(fd).st_mode)
        real_close(fd)
        if is_directory and not directory_close_failed:
            directory_close_failed = True
            raise OSError(EIO, "directory close failed")

    mocker.patch("filelock._strict.os.link", side_effect=OSError(EIO, "link failed"))
    mocker.patch("filelock._strict.os.close", side_effect=fail_directory_close)
    lock = StrictSoftFileLock(lock_path)

    with pytest.raises(BaseExceptionGroup) as raised:
        lock.acquire()
    assert ([str(error) for error in raised.value.exceptions], lock.claims, lock.is_locked) == (
        ["[Errno 5] link failed", "[Errno 5] directory close failed"],
        (),
        False,
    )


def test_strict_soft_held_directory_close_failure_leaves_both_claims(tmp_path: Path, mocker: MockerFixture) -> None:
    lock_path = tmp_path / "resource.lock"
    _initialize_protocol(lock_path)
    real_close = os.close
    real_fstat = os.fstat
    directory_closes = 0

    def fail_second_directory_close(fd: int) -> None:
        nonlocal directory_closes
        is_directory = stat.S_ISDIR(real_fstat(fd).st_mode)
        real_close(fd)
        if is_directory:
            directory_closes += 1
            if directory_closes == 2:
                raise OSError(EIO, "held directory close failed")

    mocker.patch("filelock._strict.os.close", side_effect=fail_second_directory_close)
    lock = StrictSoftFileLock(lock_path)

    with pytest.raises(OSError, match="held directory close failed"):
        lock.acquire()
    assert ([claim.state for claim in lock.claims], lock.is_locked) == (["held", "intent"], False)


@pytest.mark.parametrize("node", [pytest.param("directory", id="directory"), pytest.param("symlink", id="symlink")])
def test_strict_soft_reaper_leaves_non_regular_private_node(tmp_path: Path, node: str) -> None:
    lock_path = tmp_path / "resource.lock"
    _initialize_protocol(lock_path)
    private_path = _private_claim_path(lock_path, "0" * 32)
    if node == "directory":
        private_path.mkdir()
    else:
        private_path.symlink_to(tmp_path / "missing")

    with pytest.raises(SoftFileLockProtocolError, match="cannot list claim directory"):
        _ = StrictSoftFileLock(lock_path).claims
    assert private_path.is_dir() if node == "directory" else private_path.is_symlink()


def test_strict_soft_reaper_does_not_retry_sharing_error(tmp_path: Path, mocker: MockerFixture) -> None:
    lock_path = tmp_path / "resource.lock"
    _initialize_protocol(lock_path)
    private_path = _private_claim_path(lock_path, "0" * 32)
    private_path.write_bytes(b"abandoned")
    os.utime(private_path, (0, 0))
    unlink = mocker.patch.object(
        Path,
        "unlink",
        autospec=True,
        side_effect=PermissionError(EACCES, "sharing violation"),
    )

    started = time.perf_counter()
    assert StrictSoftFileLock(lock_path).claims == ()
    elapsed = time.perf_counter() - started
    assert (unlink.call_count, elapsed < 0.1, private_path.exists()) == (1, True, True)


def test_strict_soft_release_error_keeps_remaining_claim(tmp_path: Path, mocker: MockerFixture) -> None:
    lock_path = tmp_path / "resource.lock"
    lock = StrictSoftFileLock(lock_path)
    lock.acquire()
    claim_name = lock.claims[0].name
    real_unlink = Path.unlink
    failed = False

    def fail_once(path: Path, *, missing_ok: bool = False) -> None:
        nonlocal failed
        if not failed:
            failed = True
            assert path.name == claim_name
            raise PermissionError(EACCES, "claim is not writable")
        real_unlink(path, missing_ok=missing_ok)

    mocker.patch("filelock._strict._UNLINK_SUPPORTS_DIR_FD", new=False)
    mocker.patch.object(Path, "unlink", autospec=True, side_effect=fail_once)
    with pytest.raises(PermissionError, match="claim is not writable"):
        lock.release()
    assert (lock.is_locked, [claim.name for claim in lock.claims]) == (True, [claim_name])

    lock.release()
    assert (lock.is_locked, lock.claims) == (False, ())


def test_strict_soft_release_commits_before_directory_close_error(tmp_path: Path, mocker: MockerFixture) -> None:
    lock_path = tmp_path / "resource.lock"
    lock = StrictSoftFileLock(lock_path)
    lock.acquire()
    real_close = os.close
    real_fstat = os.fstat

    def fail_directory_close(fd: int) -> None:
        is_directory = stat.S_ISDIR(real_fstat(fd).st_mode)
        real_close(fd)
        if is_directory:
            raise OSError(EIO, "directory close failed")

    close_mock = mocker.patch("filelock._strict.os.close", side_effect=fail_directory_close)

    with pytest.raises(OSError, match="directory close failed"):
        lock.release()
    mocker.stop(close_mock)
    assert (lock.is_locked, lock.lock_counter, lock.claims) == (False, 0, ())
    with StrictSoftFileLock(lock_path, timeout=0) as contender:
        assert contender.is_locked


def test_strict_soft_release_preserves_unlink_and_directory_close_errors(tmp_path: Path, mocker: MockerFixture) -> None:
    lock_path = tmp_path / "resource.lock"
    lock = StrictSoftFileLock(lock_path)
    lock.acquire()
    real_close = os.close
    real_fstat = os.fstat
    real_unlink = os.unlink

    def fail_claim_unlink(path: _PathValue, *, dir_fd: int | None = None) -> None:
        if Path(os.fsdecode(path)).name.startswith("held-"):
            raise PermissionError(EACCES, "claim unlink failed")
        real_unlink(path, dir_fd=dir_fd)

    def fail_directory_close(fd: int) -> None:
        is_directory = stat.S_ISDIR(real_fstat(fd).st_mode)
        real_close(fd)
        if is_directory:
            raise OSError(EIO, "directory close failed")

    unlink_mock = mocker.patch("filelock._strict.os.unlink", side_effect=fail_claim_unlink)
    supports_dir_fd_mock = mocker.patch.object(os, "supports_dir_fd", os.supports_dir_fd | {unlink_mock})
    close_mock = mocker.patch("filelock._strict.os.close", side_effect=fail_directory_close)

    with pytest.raises(BaseExceptionGroup) as raised:
        lock.release()
    mocker.stop(unlink_mock)
    mocker.stop(supports_dir_fd_mock)
    mocker.stop(close_mock)
    assert (
        [str(error) for error in raised.value.exceptions],
        lock.is_locked,
        lock.lock_counter,
        [claim.state for claim in lock.claims],
    ) == (
        ["[Errno 13] claim unlink failed", "[Errno 5] directory close failed"],
        True,
        1,
        ["held"],
    )
    lock.release()


def test_strict_soft_doorway_preserves_every_claim_cleanup_error(tmp_path: Path, mocker: MockerFixture) -> None:
    lock_path = tmp_path / "resource.lock"
    _initialize_protocol(lock_path)
    owner_token = "f" * 32
    competitor_token = "0" * 32
    competitor_name = f"held-v1-{competitor_token}.claim"
    real_close = os.close
    real_fstat = os.fstat
    real_scandir = os.scandir
    real_unlink = os.unlink
    scans = 0
    cleanup_name: str | None = None

    def add_competitor_before_final_scan(path: str | os.PathLike[str]) -> Iterator[os.DirEntry[str]]:
        nonlocal scans
        scans += 1
        if scans == 3:
            Path(path, competitor_name).write_text(
                f"filelock-strict-v1\n{competitor_token}\n{os.getpid()}\n{socket.gethostname().encode().hex()}\n",
                encoding="ascii",
                newline="",
            )
        return real_scandir(path)

    def fail_held_unlink(path: _PathValue, *, dir_fd: int | None = None) -> None:
        nonlocal cleanup_name
        cleanup_name = Path(os.fsdecode(path)).name
        if cleanup_name == f"held-v1-{owner_token}.claim":
            raise PermissionError(EACCES, "held unlink failed")
        real_unlink(path, dir_fd=dir_fd)

    def fail_cleanup_directory_close(fd: int) -> None:
        is_directory = stat.S_ISDIR(real_fstat(fd).st_mode)
        real_close(fd)
        if is_directory and cleanup_name == f"intent-v1-{owner_token}.claim":
            raise OSError(EIO, "intent directory close failed")
        if is_directory and cleanup_name == f"held-v1-{owner_token}.claim":
            raise OSError(EIO, "held directory close failed")

    mocker.patch("filelock._strict.secrets.token_hex", return_value=owner_token)
    mocker.patch("filelock._strict.os.scandir", side_effect=add_competitor_before_final_scan)
    unlink_mock = mocker.patch("filelock._strict.os.unlink", side_effect=fail_held_unlink)
    supports_dir_fd_mock = mocker.patch.object(os, "supports_dir_fd", os.supports_dir_fd | {unlink_mock})
    close_mock = mocker.patch("filelock._strict.os.close", side_effect=fail_cleanup_directory_close)
    lock = StrictSoftFileLock(lock_path)

    with pytest.raises(BaseExceptionGroup) as raised:
        lock.acquire()
    mocker.stop(unlink_mock)
    mocker.stop(supports_dir_fd_mock)
    mocker.stop(close_mock)
    assert (
        _leaf_error_messages(raised.value),
        [claim.name for claim in lock.claims],
        lock.is_locked,
    ) == (
        [
            "[Errno 5] intent directory close failed",
            "[Errno 13] held unlink failed",
            "[Errno 5] held directory close failed",
        ],
        [competitor_name, f"held-v1-{owner_token}.claim"],
        True,
    )
    lock.release(force=True)
    lock.force_break(competitor_name)


@pytest.mark.parametrize(
    ("sentinel", "acquires"),
    [
        pytest.param(b"1\nfilelock-strict-v1\x00\n0\n", True, id="strict-winner"),
        pytest.param(b"legacy partial", False, id="legacy-winner"),
    ],
)
def test_strict_soft_sentinel_publication_race(
    tmp_path: Path, mocker: MockerFixture, sentinel: bytes, *, acquires: bool
) -> None:
    lock_path = tmp_path / "resource.lock"
    real_link = os.link
    raced = False

    def publish_competitor(
        source: _PathValue,
        destination: _PathValue,
        *,
        src_dir_fd: int | None = None,
        dst_dir_fd: int | None = None,
        follow_symlinks: bool = True,
    ) -> None:
        nonlocal raced
        if not raced:
            raced = True
            lock_path.write_bytes(sentinel)
            raise FileExistsError(EEXIST, "sentinel exists")
        real_link(
            source,
            destination,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
            follow_symlinks=follow_symlinks,
        )

    mocker.patch("filelock._strict.os.link", side_effect=publish_competitor)
    lock = StrictSoftFileLock(lock_path, timeout=0)

    if acquires:
        with lock:
            assert lock.is_locked
    else:
        with pytest.raises(Timeout):
            lock.acquire()
    assert lock_path.read_bytes() == sentinel


def test_strict_soft_sentinel_uses_unique_private_names(tmp_path: Path, mocker: MockerFixture) -> None:
    lock_path = tmp_path / "resource.lock"
    private_names: list[str] = []

    def reject_publication(source: _PathValue, _destination: _PathValue, **_options: int | bool | None) -> None:
        private_names.append(Path(os.fsdecode(source)).name)
        raise OSError(EIO, "publication failed")

    mocker.patch("filelock._strict.secrets.token_hex", side_effect=["0" * 32, "1" * 32])
    mocker.patch("filelock._strict.os.link", side_effect=reject_publication)

    for _attempt in range(2):
        with pytest.raises(OSError, match="publication failed"):
            StrictSoftFileLock(lock_path).acquire()
    assert private_names == [
        ".resource.lock.private-v1-00000000000000000000000000000000.tmp",
        ".resource.lock.private-v1-11111111111111111111111111111111.tmp",
    ]


def test_strict_soft_existing_non_regular_sentinel_blocks(tmp_path: Path) -> None:
    lock_path = tmp_path / "resource.lock"
    lock_path.mkdir()

    with pytest.raises(Timeout):
        StrictSoftFileLock(lock_path, timeout=0).acquire()


def test_strict_soft_sentinel_race_with_non_regular_winner_blocks(tmp_path: Path, mocker: MockerFixture) -> None:
    lock_path = tmp_path / "resource.lock"

    def publish_directory(
        _source: _PathValue,
        _destination: _PathValue,
        **_options: int | bool | None,
    ) -> None:
        lock_path.mkdir()
        raise FileExistsError(EEXIST, "sentinel exists")

    mocker.patch("filelock._strict.os.link", side_effect=publish_directory)

    with pytest.raises(Timeout):
        StrictSoftFileLock(lock_path, timeout=0).acquire()
    assert lock_path.is_dir()


def test_strict_soft_sentinel_remains_after_private_cleanup_failure(tmp_path: Path, mocker: MockerFixture) -> None:
    lock_path = tmp_path / "resource.lock"
    real_unlink = os.unlink
    failed = False

    def fail_first_private_cleanup(path: _PathValue, *, dir_fd: int | None = None) -> None:
        nonlocal failed
        if not failed and Path(os.fsdecode(path)).name.startswith(".resource.lock.private-"):
            failed = True
            raise OSError(EIO, "private cleanup failed")
        real_unlink(path, dir_fd=dir_fd)

    mocker.patch("filelock._strict.os.unlink", side_effect=fail_first_private_cleanup)

    with pytest.raises(OSError, match="private cleanup failed"):
        StrictSoftFileLock(lock_path).acquire()
    with pytest.raises(Timeout):
        SoftFileLock(lock_path, timeout=0).acquire()
    with StrictSoftFileLock(lock_path, timeout=0) as strict:
        assert (lock_path.read_bytes(), strict.is_locked) == (b"1\nfilelock-strict-v1\x00\n0\n", True)


def test_strict_soft_claim_directory_read_error_fails_closed(tmp_path: Path, mocker: MockerFixture) -> None:
    lock_path = tmp_path / "resource.lock"
    _initialize_protocol(lock_path)
    mocker.patch("filelock._strict.os.scandir", side_effect=PermissionError(EACCES, "cannot list"))

    with pytest.raises(SoftFileLockProtocolError, match="cannot list claim directory"):
        StrictSoftFileLock(lock_path).acquire()


@pytest.mark.parametrize(
    "content",
    [
        pytest.param(b"filelock-strict-v1\n" + b"0" * 32 + b"\n0\n00\n", id="zero-pid"),
        pytest.param(b"filelock-strict-v1\n" + b"1" * 32 + b"\n1\n00\n", id="wrong-token"),
    ],
)
def test_strict_soft_structurally_invalid_record_fails_closed(tmp_path: Path, content: bytes) -> None:
    lock_path = tmp_path / "resource.lock"
    claims = Path(f"{lock_path}.filelock") / "claims"
    claims.mkdir(parents=True)
    (claims / f"held-v1-{'0' * 32}.claim").write_bytes(content)

    with pytest.raises(SoftFileLockProtocolError, match="malformed claim record"):
        StrictSoftFileLock(lock_path).acquire()


def test_strict_soft_accepts_maximum_pid(tmp_path: Path) -> None:
    lock_path = tmp_path / "resource.lock"
    token = "0" * 32
    claims = Path(f"{lock_path}.filelock") / "claims"
    claims.mkdir(parents=True)
    (claims / f"held-v1-{token}.claim").write_text(
        f"filelock-strict-v1\n{token}\n4294967295\n686f7374\n",
        encoding="ascii",
        newline="",
    )

    assert StrictSoftFileLock(lock_path).claims[0].pid == 2**32 - 1


@pytest.mark.parametrize(
    ("pid", "hostname_hex"),
    [
        pytest.param("01", "686f7374", id="leading-zero-pid"),
        pytest.param("4294967296", "686f7374", id="pid-overflow"),
        pytest.param("1", "686F7374", id="uppercase-hostname-hex"),
        pytest.param("1", "20686f737420", id="spaced-hostname"),
        pytest.param("1", "686f737400", id="null-hostname"),
        pytest.param("1", "1b686f7374", id="escape-hostname"),
    ],
)
def test_strict_soft_rejects_noncanonical_owner_record(tmp_path: Path, pid: str, hostname_hex: str) -> None:
    lock_path = tmp_path / "resource.lock"
    token = "0" * 32
    claims = Path(f"{lock_path}.filelock") / "claims"
    claims.mkdir(parents=True)
    (claims / f"held-v1-{token}.claim").write_text(
        f"filelock-strict-v1\n{token}\n{pid}\n{hostname_hex}\n",
        encoding="ascii",
        newline="",
    )

    with pytest.raises(SoftFileLockProtocolError, match="malformed claim record"):
        _ = StrictSoftFileLock(lock_path).claims


@pytest.mark.parametrize(
    "node",
    [
        pytest.param("directory", id="non-regular"),
        pytest.param("oversized", id="oversized"),
    ],
)
def test_strict_soft_invalid_record_node_fails_closed(tmp_path: Path, node: str) -> None:
    lock_path = tmp_path / "resource.lock"
    claims = Path(f"{lock_path}.filelock") / "claims"
    claims.mkdir(parents=True)
    claim = claims / f"held-v1-{'0' * 32}.claim"
    if node == "directory":
        claim.mkdir()
    else:
        claim.write_bytes(b"x" * 1025)

    with pytest.raises(SoftFileLockProtocolError, match="cannot read claim"):
        StrictSoftFileLock(lock_path).acquire()


@pytest.mark.parametrize(
    ("replacement", "message"),
    [
        pytest.param("file", "changed while opening", id="different-file"),
        pytest.param("directory", "is not a regular file", id="directory"),
    ],
)
def test_strict_soft_claim_replaced_while_opening_fails_closed(
    tmp_path: Path, mocker: MockerFixture, replacement: str, message: str
) -> None:
    lock_path = tmp_path / "resource.lock"
    claims = Path(f"{lock_path}.filelock") / "claims"
    claims.mkdir(parents=True)
    claim = claims / f"held-v1-{'0' * 32}.claim"
    record = b"filelock-strict-v1\n" + b"0" * 32 + b"\n1\n686f7374\n"
    claim.write_bytes(record)
    real_open = os.open
    replaced = False

    def replace_before_open(
        path: _PathValue,
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal replaced
        if not replaced and os.fsdecode(path) == str(claim):
            replaced = True
            claim.replace(claim.with_name(f".{claim.name}.replaced"))
            if replacement == "file":
                claim.write_bytes(record)
            else:
                claim.mkdir()
        return real_open(path, flags, mode, dir_fd=dir_fd)

    mocker.patch("filelock._strict.os.open", side_effect=replace_before_open)

    with pytest.raises(SoftFileLockProtocolError, match=message):
        StrictSoftFileLock(lock_path).acquire()


def test_strict_soft_directory_inspection_error_fails_closed(tmp_path: Path, mocker: MockerFixture) -> None:
    real_mkdir = Path.mkdir

    def fail_protocol_mkdir(path: Path, mode: int = 0o777, parents: bool = False, exist_ok: bool = False) -> None:
        if str(path).endswith(".filelock"):
            raise FileExistsError
        real_mkdir(path, mode=mode, parents=parents, exist_ok=exist_ok)

    mocker.patch.object(Path, "mkdir", autospec=True, side_effect=fail_protocol_mkdir)
    mocker.patch.object(Path, "lstat", autospec=True, side_effect=PermissionError(EACCES, "cannot inspect"))

    with pytest.raises(SoftFileLockProtocolError, match="cannot inspect"):
        StrictSoftFileLock(tmp_path / "resource.lock").acquire()


def _initialize_protocol(lock_path: Path) -> None:
    with StrictSoftFileLock(lock_path):
        pass


def _private_claim_path(lock_path: Path, token: str) -> Path:
    return Path(f"{lock_path}.filelock") / "claims" / f".intent-v1-{token}.claim.private-v1-{token}.tmp"


def _leaf_error_messages(error: BaseException) -> list[str]:
    if isinstance(error, BaseExceptionGroup):
        return [message for child in error.exceptions for message in _leaf_error_messages(child)]
    return [str(error)]
