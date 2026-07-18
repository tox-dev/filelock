from __future__ import annotations

import os
import pickle  # ruff:ignore[suspicious-pickle-import]  # round-trip uses bytes produced in this test
import socket
import stat
import sys
import time
from errno import EACCES, EEXIST, EIO, ENOENT, ESTALE, EXDEV
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if sys.version_info >= (3, 11):
    from builtins import BaseExceptionGroup
else:  # pragma: no cover (<py311)
    from exceptiongroup import BaseExceptionGroup

import filelock._strict
from filelock import SoftFileLock, SoftFileLockProtocolError, StrictSoftFileLock, Timeout
from filelock._strict import _probe_link_follow_symlinks, _relative_identity

# These cases inject a failure into a directory-fd cleanup step or the reaper race, both of which only run where the
# protocol uses dir_fd descriptors. Windows has no dir_fd, so those branches never execute and the injected fault never
# fires; the win32 cleanup path is covered instead by the passing stress, contention and release tests.
_REQUIRES_DIR_FD_CLEANUP = pytest.mark.skipif(
    os.open not in os.supports_dir_fd,
    reason="injects a fault into the dir_fd cleanup path, which Windows does not execute",
)

if TYPE_CHECKING:
    from collections.abc import Iterator

    from pytest_mock import MockerFixture

_PathValue = str | bytes | os.PathLike[str] | os.PathLike[bytes]


pytestmark = pytest.mark.requires_hard_links


def test_strict_soft_protocol_error_pickles() -> None:
    error = SoftFileLockProtocolError("resource.lock", "held-v2-claim", "unknown version")

    restored = pickle.loads(pickle.dumps(error))  # ruff:ignore[suspicious-pickle-usage]  # input comes from the preceding local value

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


@_REQUIRES_DIR_FD_CLEANUP  # pragma: win32 no cover
def test_strict_soft_reaper_removes_private_before_publication(tmp_path: Path, mocker: MockerFixture) -> None:
    lock_path = tmp_path / "resource.lock"
    _initialize_protocol(lock_path)
    token = "0" * 32
    private_path = _private_claim_path(lock_path, token)
    real_link = os.link

    def reap_before_link(  # pragma: win32 no cover
        source: _PathValue,
        destination: _PathValue,
        *,
        src_dir_fd: int | None = None,
        dst_dir_fd: int | None = None,
        follow_symlinks: bool = True,
    ) -> None:
        if Path(os.fsdecode(destination)).name.startswith("intent-"):  # pragma: win32 no cover
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

    with pytest.raises(Timeout):  # pragma: win32 no cover
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


@_REQUIRES_DIR_FD_CLEANUP  # pragma: win32 no cover
def test_strict_soft_reaper_replacement_only_aborts_publisher(tmp_path: Path, mocker: MockerFixture) -> None:
    lock_path = tmp_path / "resource.lock"
    _initialize_protocol(lock_path)
    token = "0" * 32
    private_path = _private_claim_path(lock_path, token)
    displaced_path = private_path.with_suffix(".displaced")
    real_link = os.link
    real_unlink = Path.unlink
    replaced = False

    def replace_before_unlink(path: Path, *, missing_ok: bool = False) -> None:  # pragma: win32 no cover
        nonlocal replaced
        if path == private_path and not replaced:  # pragma: win32 no cover
            replaced = True
            path.replace(displaced_path)
            path.write_bytes(b"replacement publisher")
            return
        real_unlink(path, missing_ok=missing_ok)

    def reap_before_link(  # pragma: win32 no cover
        source: _PathValue,
        destination: _PathValue,
        *,
        src_dir_fd: int | None = None,
        dst_dir_fd: int | None = None,
        follow_symlinks: bool = True,
    ) -> None:
        if Path(os.fsdecode(destination)).name.startswith("intent-"):  # pragma: win32 no cover
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

    with pytest.raises(Timeout):  # pragma: win32 no cover
        lock.acquire()
    with pytest.raises(SoftFileLockProtocolError, match="malformed claim record"):  # pragma: win32 no cover
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


@_REQUIRES_DIR_FD_CLEANUP  # pragma: win32 no cover
def test_strict_soft_link_and_directory_close_failures_preserve_both(tmp_path: Path, mocker: MockerFixture) -> None:
    lock_path = tmp_path / "resource.lock"
    _initialize_protocol(lock_path)
    real_close = os.close
    real_fstat = os.fstat
    directory_close_failed = False

    def fail_directory_close(fd: int) -> None:  # pragma: win32 no cover
        nonlocal directory_close_failed
        is_directory = stat.S_ISDIR(real_fstat(fd).st_mode)
        real_close(fd)
        if is_directory and not directory_close_failed:  # pragma: win32 no cover
            directory_close_failed = True
            raise OSError(EIO, "directory close failed")

    mocker.patch("filelock._strict.os.link", side_effect=OSError(EIO, "link failed"))
    mocker.patch("filelock._strict.os.close", side_effect=fail_directory_close)
    lock = StrictSoftFileLock(lock_path)

    with pytest.raises(BaseExceptionGroup) as raised:  # pragma: win32 no cover
        lock.acquire()
    assert ([str(error) for error in raised.value.exceptions], lock.claims, lock.is_locked) == (
        ["[Errno 5] link failed", "[Errno 5] directory close failed"],
        (),
        False,
    )


@_REQUIRES_DIR_FD_CLEANUP  # pragma: win32 no cover
def test_strict_soft_held_directory_close_failure_leaves_both_claims(tmp_path: Path, mocker: MockerFixture) -> None:
    lock_path = tmp_path / "resource.lock"
    _initialize_protocol(lock_path)
    real_close = os.close
    real_fstat = os.fstat
    directory_closes = 0

    def fail_second_directory_close(fd: int) -> None:  # pragma: win32 no cover
        nonlocal directory_closes
        is_directory = stat.S_ISDIR(real_fstat(fd).st_mode)
        real_close(fd)
        if is_directory:  # pragma: win32 no cover
            directory_closes += 1
            if directory_closes == 2:  # pragma: win32 no cover
                raise OSError(EIO, "held directory close failed")

    mocker.patch("filelock._strict.os.close", side_effect=fail_second_directory_close)
    lock = StrictSoftFileLock(lock_path)

    with pytest.raises(OSError, match="held directory close failed"):  # pragma: win32 no cover
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


@_REQUIRES_DIR_FD_CLEANUP  # pragma: win32 no cover
def test_strict_soft_release_error_keeps_remaining_claim(tmp_path: Path, mocker: MockerFixture) -> None:
    lock_path = tmp_path / "resource.lock"
    lock = StrictSoftFileLock(lock_path)
    lock.acquire()
    claim_name = lock.claims[0].name
    real_unlink = Path.unlink
    failed = False

    def fail_once(path: Path, *, missing_ok: bool = False) -> None:  # pragma: win32 no cover
        nonlocal failed
        if not failed:  # pragma: win32 no cover
            failed = True
            assert path.name == claim_name
            raise PermissionError(EACCES, "claim is not writable")
        real_unlink(path, missing_ok=missing_ok)

    mocker.patch("filelock._strict._UNLINK_SUPPORTS_DIR_FD", new=False)
    mocker.patch.object(Path, "unlink", autospec=True, side_effect=fail_once)
    with pytest.raises(PermissionError, match="claim is not writable"):  # pragma: win32 no cover
        lock.release()
    assert (lock.is_locked, [claim.name for claim in lock.claims]) == (True, [claim_name])

    lock.release()
    assert (lock.is_locked, lock.claims) == (False, ())


@_REQUIRES_DIR_FD_CLEANUP  # pragma: win32 no cover
def test_strict_soft_release_commits_before_directory_close_error(tmp_path: Path, mocker: MockerFixture) -> None:
    lock_path = tmp_path / "resource.lock"
    lock = StrictSoftFileLock(lock_path)
    lock.acquire()
    real_close = os.close
    real_fstat = os.fstat
    directory_close_failed = False

    def fail_directory_close(fd: int) -> None:  # pragma: win32 no cover
        nonlocal directory_close_failed
        is_directory = stat.S_ISDIR(real_fstat(fd).st_mode)
        real_close(fd)
        if is_directory and not directory_close_failed:  # pragma: win32 no cover
            directory_close_failed = True
            raise OSError(EIO, "directory close failed")

    close_mock = mocker.patch("filelock._strict.os.close", side_effect=fail_directory_close)

    with pytest.raises(OSError, match="directory close failed"):  # pragma: win32 no cover
        lock.release()
    mocker.stop(close_mock)
    assert (lock.is_locked, lock.lock_counter, lock.claims) == (False, 0, ())
    with StrictSoftFileLock(lock_path, timeout=0) as contender:  # pragma: win32 no cover
        assert contender.is_locked


@_REQUIRES_DIR_FD_CLEANUP  # pragma: win32 no cover
def test_strict_soft_release_preserves_unlink_and_directory_close_errors(tmp_path: Path, mocker: MockerFixture) -> None:
    lock_path = tmp_path / "resource.lock"
    lock = StrictSoftFileLock(lock_path)
    lock.acquire()
    real_close = os.close
    real_fstat = os.fstat
    real_unlink = os.unlink
    directory_close_failed = False

    def fail_claim_unlink(path: _PathValue, *, dir_fd: int | None = None) -> None:  # pragma: win32 no cover
        if Path(os.fsdecode(path)).name.startswith("held-"):  # pragma: win32 no cover
            raise PermissionError(EACCES, "claim unlink failed")
        real_unlink(path, dir_fd=dir_fd)

    def fail_directory_close(fd: int) -> None:  # pragma: win32 no cover
        nonlocal directory_close_failed
        is_directory = stat.S_ISDIR(real_fstat(fd).st_mode)
        real_close(fd)
        if is_directory and not directory_close_failed:  # pragma: win32 no cover
            directory_close_failed = True
            raise OSError(EIO, "directory close failed")

    unlink_mock = mocker.patch("filelock._strict.os.unlink", side_effect=fail_claim_unlink)
    supports_dir_fd_mock = mocker.patch.object(os, "supports_dir_fd", os.supports_dir_fd | {unlink_mock})
    close_mock = mocker.patch("filelock._strict.os.close", side_effect=fail_directory_close)

    with pytest.raises(BaseExceptionGroup) as raised:  # pragma: win32 no cover
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


@_REQUIRES_DIR_FD_CLEANUP  # pragma: win32 no cover
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
        if scans == 3:  # pragma: win32 no cover
            Path(path, competitor_name).write_text(
                f"filelock-strict-v1\n{competitor_token}\n{os.getpid()}\n{socket.gethostname().encode().hex()}\n4242\n",
                encoding="ascii",
                newline="",
            )
        return real_scandir(path)

    def fail_held_unlink(path: _PathValue, *, dir_fd: int | None = None) -> None:  # pragma: win32 no cover
        nonlocal cleanup_name
        cleanup_name = Path(os.fsdecode(path)).name
        if cleanup_name == f"held-v1-{owner_token}.claim":  # pragma: win32 no cover
            raise PermissionError(EACCES, "held unlink failed")
        real_unlink(path, dir_fd=dir_fd)

    def fail_cleanup_directory_close(fd: int) -> None:  # pragma: win32 no cover
        is_directory = stat.S_ISDIR(real_fstat(fd).st_mode)
        real_close(fd)
        if is_directory and cleanup_name == f"intent-v1-{owner_token}.claim":  # pragma: win32 no cover
            raise OSError(EIO, "intent directory close failed")
        if is_directory and cleanup_name == f"held-v1-{owner_token}.claim":  # pragma: win32 no cover
            raise OSError(EIO, "held directory close failed")

    mocker.patch("filelock._strict.secrets.token_hex", return_value=owner_token)
    mocker.patch("filelock._strict.os.scandir", side_effect=add_competitor_before_final_scan)
    unlink_mock = mocker.patch("filelock._strict.os.unlink", side_effect=fail_held_unlink)
    supports_dir_fd_mock = mocker.patch.object(os, "supports_dir_fd", os.supports_dir_fd | {unlink_mock})
    close_mock = mocker.patch("filelock._strict.os.close", side_effect=fail_cleanup_directory_close)
    lock = StrictSoftFileLock(lock_path)

    with pytest.raises(BaseExceptionGroup) as raised:  # pragma: win32 no cover
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
            "[Errno 13] held unlink failed",
            "[Errno 5] held directory close failed",
            "[Errno 5] intent directory close failed",
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


@_REQUIRES_DIR_FD_CLEANUP  # pragma: win32 no cover
def test_strict_soft_sentinel_remains_after_private_cleanup_failure(tmp_path: Path, mocker: MockerFixture) -> None:
    lock_path = tmp_path / "resource.lock"
    real_unlink = os.unlink
    failed = False

    def fail_first_private_cleanup(path: _PathValue, *, dir_fd: int | None = None) -> None:  # pragma: win32 no cover
        nonlocal failed
        if not failed and Path(os.fsdecode(path)).name.startswith(".resource.lock.private-"):  # pragma: win32 no cover
            failed = True
            raise OSError(EIO, "private cleanup failed")
        real_unlink(path, dir_fd=dir_fd)

    mocker.patch("filelock._strict.os.unlink", side_effect=fail_first_private_cleanup)

    with pytest.raises(OSError, match="private cleanup failed"):  # pragma: win32 no cover
        StrictSoftFileLock(lock_path).acquire()
    with pytest.raises(Timeout):  # pragma: win32 no cover
        SoftFileLock(lock_path, timeout=0).acquire()
    with StrictSoftFileLock(lock_path, timeout=0) as strict:  # pragma: win32 no cover
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
        pytest.param(b"filelock-strict-v1\n" + b"0" * 32 + b"\n0\n00\n4242\n", id="zero-pid"),
        pytest.param(b"filelock-strict-v1\n" + b"1" * 32 + b"\n1\n00\n4242\n", id="wrong-token"),
        pytest.param(b"filelock-strict-v1\n" + b"0" * 32 + b"\n1\n686f7374\nnot-int\n", id="non-integer-start"),
        pytest.param(b"filelock-strict-v1\n" + b"0" * 32 + b"\n1\n686f7374\n-5\n", id="negative-start"),
        pytest.param(b"filelock-strict-v1\n" + b"0" * 32 + b"\n1\n686f7374\n007\n", id="noncanonical-start"),
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
        f"filelock-strict-v1\n{token}\n2147483647\n686f7374\n4242\n",
        encoding="ascii",
        newline="",
    )

    assert StrictSoftFileLock(lock_path).claims[0].pid == 2**31 - 1


@pytest.mark.parametrize(
    ("pid", "hostname_hex"),
    [
        pytest.param("01", "686f7374", id="leading-zero-pid"),
        pytest.param("2147483648", "686f7374", id="pid-above-signed-int"),
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
        f"filelock-strict-v1\n{token}\n{pid}\n{hostname_hex}\n4242\n",
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


def _write_held_claim(claims: Path) -> Path:
    claims.mkdir(parents=True)
    claim = claims / f"held-v1-{'0' * 32}.claim"
    claim.write_bytes(b"filelock-strict-v1\n" + b"0" * 32 + b"\n1\n686f7374\n\n")
    return claim


def _open_raising_for(claim: Path, error: OSError, mocker: MockerFixture) -> None:
    real_open = os.open

    def failing_open(path: _PathValue, flags: int, mode: int = 0o777, *, dir_fd: int | None = None) -> int:
        if os.fsdecode(path) == str(claim):
            raise error
        return real_open(path, flags, mode, dir_fd=dir_fd)

    mocker.patch("filelock._strict.os.open", side_effect=failing_open)


def test_strict_soft_stale_claim_read_is_skipped(tmp_path: Path, mocker: MockerFixture) -> None:
    # An NFS peer that unlinks its own claim leaves this client's cached filehandle stale, so the open returns ESTALE
    # rather than ENOENT. A stale handle that outlives revalidation is a vanished claim, not an unreadable one.
    lock_path = tmp_path / "resource.lock"
    claim = _write_held_claim(Path(f"{lock_path}.filelock") / "claims")
    mocker.patch("filelock._strict._CLAIM_READ_GRACE", 0.02)
    _open_raising_for(claim, OSError(ESTALE, "Stale file handle"), mocker)

    assert StrictSoftFileLock(lock_path).claims == ()


def test_strict_soft_stale_claim_read_revalidates_to_gone(tmp_path: Path, mocker: MockerFixture) -> None:
    lock_path = tmp_path / "resource.lock"
    claim = _write_held_claim(Path(f"{lock_path}.filelock") / "claims")
    real_open = os.open
    stale_raised = False

    def stale_then_vanish(path: _PathValue, flags: int, mode: int = 0o777, *, dir_fd: int | None = None) -> int:
        nonlocal stale_raised
        if not stale_raised and os.fsdecode(path) == str(claim):
            stale_raised = True
            claim.unlink()
            raise OSError(ESTALE, "Stale file handle")
        return real_open(path, flags, mode, dir_fd=dir_fd)

    mocker.patch("filelock._strict.os.open", side_effect=stale_then_vanish)

    assert StrictSoftFileLock(lock_path).claims == ()
    assert stale_raised


def test_strict_soft_io_error_claim_read_fails_closed(tmp_path: Path, mocker: MockerFixture) -> None:
    lock_path = tmp_path / "resource.lock"
    claim = _write_held_claim(Path(f"{lock_path}.filelock") / "claims")
    _open_raising_for(claim, OSError(EIO, "I/O error"), mocker)

    with pytest.raises(SoftFileLockProtocolError, match="cannot read claim"):
        _ = StrictSoftFileLock(lock_path).claims


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


def _leaf_error_messages(error: BaseException) -> list[str]:  # pragma: win32 no cover
    if isinstance(error, BaseExceptionGroup):  # pragma: win32 no cover
        return [message for child in error.exceptions for message in _leaf_error_messages(child)]
    return [str(error)]


@pytest.mark.parametrize(
    "error",
    [
        pytest.param(OSError(EACCES, "denied"), id="oserror"),
        pytest.param(NotImplementedError("no follow_symlinks"), id="notimplemented"),
        pytest.param(ValueError("bad option"), id="valueerror"),
    ],
)
def test_strict_soft_link_follow_probe_reports_unhonored(mocker: MockerFixture, error: Exception) -> None:
    mocker.patch("filelock._strict.os.link", side_effect=error)

    assert _probe_link_follow_symlinks() is False


def test_strict_soft_sentinel_inspection_and_close_failure_group(tmp_path: Path, mocker: MockerFixture) -> None:
    lock_path = tmp_path / "resource.lock"
    _initialize_protocol(lock_path)
    real_fstat = os.fstat
    real_close = os.close
    fstats = 0
    close_failed = False

    def fail_sentinel_inspection(fd: int) -> os.stat_result:
        nonlocal fstats
        fstats += 1
        if fstats == 2:
            raise OSError(EIO, "sentinel inspection failed")
        return real_fstat(fd)

    def fail_first_close(fd: int) -> None:
        nonlocal close_failed
        if not close_failed:
            close_failed = True
            raise OSError(EIO, "sentinel close failed")
        real_close(fd)

    mocker.patch("filelock._strict.os.fstat", side_effect=fail_sentinel_inspection)
    mocker.patch("filelock._strict.os.close", side_effect=fail_first_close)
    lock = StrictSoftFileLock(lock_path)

    with pytest.raises(BaseExceptionGroup) as raised:
        lock.acquire()
    assert ([str(error) for error in raised.value.exceptions], lock.is_locked) == (
        ["[Errno 5] sentinel inspection failed", "[Errno 5] sentinel close failed"],
        False,
    )


@_REQUIRES_DIR_FD_CLEANUP  # pragma: win32 no cover
@pytest.mark.parametrize("initialized", [pytest.param(False, id="sentinel"), pytest.param(True, id="intent")])
def test_strict_soft_publication_directory_close_failure(
    tmp_path: Path, mocker: MockerFixture, initialized: bool
) -> None:
    lock_path = tmp_path / "resource.lock"
    if initialized:
        _initialize_protocol(lock_path)
    real_close = os.close
    real_fstat = os.fstat
    closed = False

    def fail_first_directory_close(fd: int) -> None:  # pragma: win32 no cover
        nonlocal closed
        is_directory = stat.S_ISDIR(real_fstat(fd).st_mode)
        real_close(fd)
        if is_directory and not closed:  # pragma: win32 no cover
            closed = True
            raise OSError(EIO, "publication directory close failed")

    mocker.patch("filelock._strict.os.close", side_effect=fail_first_directory_close)
    lock = StrictSoftFileLock(lock_path, timeout=0)

    with pytest.raises(OSError, match="publication directory close failed"):  # pragma: win32 no cover
        lock.acquire()
    assert lock.is_locked is False


@_REQUIRES_DIR_FD_CLEANUP  # pragma: win32 no cover
def test_strict_soft_force_break_directory_close_failure(tmp_path: Path, mocker: MockerFixture) -> None:
    lock_path = tmp_path / "resource.lock"
    lock = StrictSoftFileLock(lock_path)
    lock.acquire()
    claim_name = lock.claims[0].name
    real_close = os.close
    real_fstat = os.fstat
    closed = False

    def fail_directory_close(fd: int) -> None:  # pragma: win32 no cover
        nonlocal closed
        is_directory = stat.S_ISDIR(real_fstat(fd).st_mode)
        real_close(fd)
        if is_directory and not closed:  # pragma: win32 no cover
            closed = True
            raise OSError(EIO, "force break directory close failed")

    mocker.patch("filelock._strict.os.close", side_effect=fail_directory_close)

    with pytest.raises(OSError, match="force break directory close failed"):  # pragma: win32 no cover
        lock.force_break(claim_name)
    lock.release()
    assert (lock.is_locked, lock.claims) == (False, ())


def test_strict_soft_release_sentinel_close_failure(tmp_path: Path, mocker: MockerFixture) -> None:
    lock_path = tmp_path / "resource.lock"
    lock = StrictSoftFileLock(lock_path)
    lock.acquire()
    sentinel_fd = lock._context.lock_file_fd
    real_close = os.close

    def fail_sentinel_close(fd: int) -> None:
        if fd == sentinel_fd:
            raise OSError(EIO, "sentinel close failed")
        real_close(fd)

    mocker.patch("filelock._strict.os.close", side_effect=fail_sentinel_close)

    with pytest.raises(OSError, match="sentinel close failed"):
        lock.release()
    assert (lock.is_locked, lock.claims) == (False, ())


def test_strict_soft_discard_sentinel_close_failure(tmp_path: Path, mocker: MockerFixture) -> None:
    lock_path = tmp_path / "resource.lock"
    _initialize_protocol(lock_path)
    _write_foreign_claim(Path(f"{lock_path}.filelock") / "claims", "a" * 32)
    real_close = os.close
    closes = 0

    def fail_second_close(fd: int) -> None:
        nonlocal closes
        closes += 1
        if closes == 2:
            raise OSError(EIO, "doorway sentinel close failed")
        real_close(fd)

    mocker.patch("filelock._strict.os.close", side_effect=fail_second_close)
    lock = StrictSoftFileLock(lock_path)

    with pytest.raises(OSError, match="doorway sentinel close failed"):
        lock.acquire()
    assert (lock.is_locked, [claim.state for claim in lock.claims]) == (False, ["held"])


@pytest.mark.parametrize("initialized", [pytest.param(False, id="sentinel"), pytest.param(True, id="intent")])
def test_strict_soft_publication_record_replaced_aborts(
    tmp_path: Path, mocker: MockerFixture, initialized: bool
) -> None:
    lock_path = tmp_path / "resource.lock"
    if initialized:
        _initialize_protocol(lock_path)
    real_identity = _relative_identity

    def mismatch_public(directory_ref: tuple[str, int | None], name: str) -> tuple[int, int] | None:
        identity = real_identity(directory_ref, name)
        if identity is not None and not name.startswith("."):
            return identity[0], identity[1] + 1
        return identity

    mocker.patch("filelock._strict._relative_identity", side_effect=mismatch_public)
    lock = StrictSoftFileLock(lock_path, timeout=0)

    with pytest.raises(Timeout):
        lock.acquire()
    assert lock.is_locked is False


def test_strict_soft_publication_record_reclaimed_aborts(tmp_path: Path, mocker: MockerFixture) -> None:
    lock_path = tmp_path / "resource.lock"
    _initialize_protocol(lock_path)
    real_identity = _relative_identity

    def source_gone(directory_ref: tuple[str, int | None], name: str) -> tuple[int, int] | None:
        if name.startswith("."):
            return None
        return real_identity(directory_ref, name)

    mocker.patch("filelock._strict._relative_identity", side_effect=source_gone)
    mocker.patch("filelock._strict._link_relative", side_effect=FileNotFoundError(ENOENT, "source vanished"))
    lock = StrictSoftFileLock(lock_path, timeout=0)

    with pytest.raises(Timeout):
        lock.acquire()
    assert lock.is_locked is False


def test_strict_soft_private_inspection_failure_unlinks_vanished_record(tmp_path: Path, mocker: MockerFixture) -> None:
    lock_path = tmp_path / "resource.lock"
    _initialize_protocol(lock_path)
    real_fstat = os.fstat
    fstats = 0

    def fail_private_inspection(fd: int) -> os.stat_result:
        nonlocal fstats
        fstats += 1
        if fstats == 3:
            raise OSError(EIO, "private inspection failed")
        return real_fstat(fd)

    mocker.patch("filelock._strict.os.fstat", side_effect=fail_private_inspection)
    mocker.patch("filelock._strict._unlink_relative", side_effect=FileNotFoundError(ENOENT, "record gone"))
    lock = StrictSoftFileLock(lock_path)

    with pytest.raises(OSError, match="private inspection failed"):
        lock.acquire()
    assert lock.is_locked is False


def test_strict_soft_record_finalization_close_and_unlink_failures_group(tmp_path: Path, mocker: MockerFixture) -> None:
    lock_path = tmp_path / "resource.lock"
    _initialize_protocol(lock_path)
    real_close = os.close
    close_failed = False

    def fail_first_close(fd: int) -> None:
        nonlocal close_failed
        real_close(fd)
        if not close_failed:
            close_failed = True
            raise OSError(EIO, "private close failed")

    mocker.patch("filelock._strict.os.close", side_effect=fail_first_close)
    mocker.patch("filelock._strict._unlink_relative", side_effect=PermissionError(EACCES, "unlink denied"))
    lock = StrictSoftFileLock(lock_path)

    with pytest.raises(BaseExceptionGroup) as raised:
        lock.acquire()
    assert (sorted(str(error) for error in raised.value.exceptions), lock.is_locked) == (
        ["[Errno 13] unlink denied", "[Errno 5] private close failed"],
        False,
    )


def test_strict_soft_release_claim_unlink_failures_group(tmp_path: Path, mocker: MockerFixture) -> None:
    lock_path = tmp_path / "resource.lock"
    lock = StrictSoftFileLock(lock_path)
    lock.acquire()
    unlink_mock = mocker.patch(
        "filelock._strict._unlink_in_directory",
        side_effect=PermissionError(EACCES, "claim unlink denied"),
    )

    with pytest.raises(BaseExceptionGroup) as raised:
        lock.release()
    assert ([str(error) for error in raised.value.exceptions], lock.is_locked, len(lock.claims)) == (
        ["[Errno 13] claim unlink denied", "[Errno 13] claim unlink denied"],
        True,
        2,
    )

    mocker.stop(unlink_mock)
    lock.release()
    assert (lock.is_locked, lock.claims) == (False, ())


def test_strict_soft_doorway_claim_unlink_failure_commits_owned(tmp_path: Path, mocker: MockerFixture) -> None:
    lock_path = tmp_path / "resource.lock"
    _initialize_protocol(lock_path)
    owner_token = "f" * 32
    competitor_name = f"held-v1-{'0' * 32}.claim"
    intent_name = f"intent-v1-{owner_token}.claim"
    real_scandir = os.scandir
    injected = False

    def add_competitor_once_intent_is_published(path: str | os.PathLike[str]) -> Iterator[os.DirEntry[str]]:
        # Key on directory content, not scandir call count: the setup and publication paths scan a differing number
        # of times per platform, so plant the held competitor on the first rescan that already lists our own intent.
        nonlocal injected
        directory = Path(os.fsdecode(path))
        if not injected:
            with real_scandir(directory) as entries:
                intent_published = any(entry.name == intent_name for entry in entries)
            if intent_published:
                _write_foreign_claim(directory, "0" * 32)
                injected = True
        return real_scandir(directory)

    real_unlink_in_directory = filelock._strict._unlink_in_directory

    def deny_only_the_owned_intent_unlink(directory: Path, name: str) -> BaseException | None:
        # Fail only the doorway's own intent-claim unlink. A blanket failure also breaks the Windows-only private
        # record cleanup inside publication (_unlink_relative unlinks a differently named private file there), firing
        # the fault before the doorway ever discards and masking the commit-owned path this test targets.
        if name == intent_name:
            raise PermissionError(EACCES, "intent unlink denied")
        return real_unlink_in_directory(directory, name)

    mocker.patch("filelock._strict.secrets.token_hex", return_value=owner_token)
    scandir_mock = mocker.patch("filelock._strict.os.scandir", side_effect=add_competitor_once_intent_is_published)
    unlink_mock = mocker.patch("filelock._strict._unlink_in_directory", side_effect=deny_only_the_owned_intent_unlink)
    lock = StrictSoftFileLock(lock_path)

    with pytest.raises(PermissionError, match="intent unlink denied"):
        lock.acquire()
    assert (lock.is_locked, sorted(claim.name for claim in lock.claims)) == (
        True,
        [competitor_name, f"intent-v1-{owner_token}.claim"],
    )

    mocker.stop(scandir_mock)
    mocker.stop(unlink_mock)
    lock.release(force=True)
    lock.force_break(competitor_name)


def test_strict_soft_permission_denied_claim_read_fails_closed(tmp_path: Path, mocker: MockerFixture) -> None:
    lock_path = tmp_path / "resource.lock"
    claim = _write_held_claim(Path(f"{lock_path}.filelock") / "claims")
    mocker.patch("filelock._strict._CLAIM_READ_GRACE", 0.02)
    _open_raising_for(claim, PermissionError(EACCES, "sharing violation"), mocker)

    with pytest.raises(SoftFileLockProtocolError, match="cannot read claim"):
        _ = StrictSoftFileLock(lock_path).claims


def test_strict_soft_ignores_malformed_private_record_name(tmp_path: Path) -> None:
    lock_path = tmp_path / "resource.lock"
    claims = Path(f"{lock_path}.filelock") / "claims"
    claims.mkdir(parents=True)
    (claims / ".decoy.tmp").write_bytes(b"junk")

    assert StrictSoftFileLock(lock_path).claims == ()


def test_strict_soft_record_read_and_close_failure_group(tmp_path: Path, mocker: MockerFixture) -> None:
    lock_path = tmp_path / "resource.lock"
    claims = Path(f"{lock_path}.filelock") / "claims"
    claims.mkdir(parents=True)
    (claims / f"held-v1-{'0' * 32}.claim").write_bytes(b"x" * 1025)
    real_close = os.close
    close_failed = False

    def fail_first_close(fd: int) -> None:
        nonlocal close_failed
        real_close(fd)
        if not close_failed:
            close_failed = True
            raise OSError(EIO, "record close failed")

    mocker.patch("filelock._strict.os.close", side_effect=fail_first_close)

    with pytest.raises(BaseExceptionGroup) as raised:
        _ = StrictSoftFileLock(lock_path).claims
    messages = [str(error) for error in raised.value.exceptions]
    assert (
        any("exceeds 1024 bytes" in message for message in messages),
        "[Errno 5] record close failed" in messages,
    ) == (
        True,
        True,
    )


def test_strict_soft_acquires_without_dir_fd(tmp_path: Path, mocker: MockerFixture) -> None:
    lock_path = tmp_path / "resource.lock"
    mocker.patch("filelock._strict._OPEN_SUPPORTS_DIR_FD", new=False)
    mocker.patch("filelock._strict._UNLINK_SUPPORTS_DIR_FD", new=False)
    mocker.patch("filelock._strict._STAT_SUPPORTS_DIR_FD", new=False)
    mocker.patch("filelock._strict._LINK_SUPPORTS_DIR_FD", new=False)

    with StrictSoftFileLock(lock_path) as lock:
        assert (lock.is_locked, [claim.state for claim in lock.claims]) == (True, ["held", "intent"])
    assert StrictSoftFileLock(lock_path).claims == ()


@_REQUIRES_DIR_FD_CLEANUP  # pragma: win32 no cover
def test_strict_soft_held_link_and_directory_close_failure(tmp_path: Path, mocker: MockerFixture) -> None:
    lock_path = tmp_path / "resource.lock"
    _initialize_protocol(lock_path)
    real_close = os.close
    real_fstat = os.fstat
    real_link = os.link
    held_failed = False
    directory_closed = False

    def fail_held_link(  # pragma: win32 no cover
        source: _PathValue,
        destination: _PathValue,
        *,
        src_dir_fd: int | None = None,
        dst_dir_fd: int | None = None,
        follow_symlinks: bool = True,
    ) -> None:
        nonlocal held_failed
        if Path(os.fsdecode(destination)).name.startswith("held-"):  # pragma: win32 no cover
            held_failed = True
            raise OSError(EIO, "held link failed")
        real_link(source, destination, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd, follow_symlinks=follow_symlinks)

    def fail_directory_close_after_held(fd: int) -> None:  # pragma: win32 no cover
        nonlocal directory_closed
        is_directory = stat.S_ISDIR(real_fstat(fd).st_mode)
        real_close(fd)
        if is_directory and held_failed and not directory_closed:  # pragma: win32 no cover
            directory_closed = True
            raise OSError(EIO, "held link directory close failed")

    mocker.patch("filelock._strict.os.link", side_effect=fail_held_link)
    mocker.patch("filelock._strict.os.close", side_effect=fail_directory_close_after_held)
    lock = StrictSoftFileLock(lock_path)

    with pytest.raises(BaseExceptionGroup) as raised:  # pragma: win32 no cover
        lock.acquire()
    assert ([str(error) for error in raised.value.exceptions], lock.is_locked, lock.claims) == (
        ["[Errno 5] held link failed", "[Errno 5] held link directory close failed"],
        False,
        (),
    )


def test_strict_soft_opened_record_non_regular_fails_closed(tmp_path: Path, mocker: MockerFixture) -> None:
    lock_path = tmp_path / "resource.lock"
    claims = Path(f"{lock_path}.filelock") / "claims"
    claims.mkdir(parents=True)
    (claims / f"held-v1-{'0' * 32}.claim").write_bytes(b"filelock-strict-v1\n" + b"0" * 32 + b"\n1\n686f7374\n4242\n")
    real_fstat = os.fstat

    def non_regular_fstat(fd: int) -> os.stat_result:
        original = real_fstat(fd)
        return os.stat_result((stat.S_IFIFO | 0o644, *original[1:]))

    mocker.patch("filelock._strict.os.fstat", side_effect=non_regular_fstat)

    with pytest.raises(SoftFileLockProtocolError, match="is not a regular file"):
        _ = StrictSoftFileLock(lock_path).claims


def _write_foreign_claim(claims: Path, token: str, state: str = "held") -> Path:
    claim = claims / f"{state}-v1-{token}.claim"
    claim.write_bytes(b"filelock-strict-v1\n" + token.encode() + f"\n{os.getpid()}\n686f7374\n4242\n".encode())
    return claim
