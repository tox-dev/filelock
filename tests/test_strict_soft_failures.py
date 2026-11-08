from __future__ import annotations

import gc
import os
import pickle  # ruff:ignore[suspicious-pickle-import]  # round-trip uses bytes produced in this test
import re
import secrets
import socket
import stat
import subprocess  # ruff:ignore[suspicious-subprocess-import]  # a finished child yields a PID that no longer runs
import sys
import threading
import time
from errno import EACCES, EEXIST, EIO, ENOENT, ENOSPC, ESTALE, EXDEV
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Final, NoReturn, Protocol

import pytest
from capabilities import CAPABILITIES

import filelock._strict
from filelock import MarkerSoftFileLock, SoftFileLock, SoftFileLockProtocolError, StrictSoftFileLock, Timeout
from filelock._identity import host_name
from filelock._strict import _PRIVATE_RECORD_MARKER, _probe_link_follow_symlinks, _relative_identity
from tests.capability_marks import (
    NEEDS_COLLECTED_FINALIZATION,
    NEEDS_FOREIGN_STICKY_TMP,
    NEEDS_SYMLINK,
    NEEDS_UNLINK_OPEN_FILE,
)
from tests.descriptor_helpers import descriptors_on

# These cases inject a failure into a directory-fd cleanup step or the reaper race, both of which only run where the
# protocol uses dir_fd descriptors. Without dir_fd those branches never execute and the injected fault never fires; the
# cleanup path is covered there instead by the passing stress, contention and release tests.
_NEEDS_DIR_FD: Final[pytest.MarkDecorator] = pytest.mark.skipif(
    os.open not in os.supports_dir_fd,
    reason="injects a fault into the dir_fd cleanup path, which a runtime without dir_fd never executes",
)

# Narrower still: GraalPy takes os.open relative to a directory descriptor but not os.link, so the backend never opens
# the directory these two faults are counted against.
_NEEDS_LINK_DIR_FD: Final[pytest.MarkDecorator] = pytest.mark.skipif(
    not CAPABILITIES["link-dir-fd"],
    reason="counts the directory the dir_fd link path opens, which a runtime without link dir_fd never opens",
)

_NEEDS_FD_DIRECTORY: Final[pytest.MarkDecorator] = pytest.mark.skipif(
    not CAPABILITIES["fd-directory"], reason="counting open descriptors needs a /dev/fd or /proc/self/fd view"
)

# Only NTFS refuses every open of a name whose deletion is pending while the deleting handle stays open.
_WINDOWS_ONLY: Final[pytest.MarkDecorator] = pytest.mark.skipif(sys.platform != "win32", reason="Windows only")

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from pytest_mock import MockerFixture, MockType

_PathValue = str | bytes | os.PathLike[str] | os.PathLike[bytes]


class _Inspect(Protocol):
    def __call__(self, path: _PathValue, **options: int | bool | None) -> os.stat_result: ...


_UNPARSABLE_CONTENTS: Final[list[pytest.ParameterSet]] = [
    pytest.param(b"", id="pre-3.22-empty"),
    pytest.param(b"4242\nhost", id="cut"),
]


pytestmark: Final[pytest.MarkDecorator] = pytest.mark.requires_hard_links


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
        _destination: _PathValue,
        **_options: int | bool | None,
    ) -> None:
        # The intent link is the only link this flow attempts before it fails closed, so colliding it unconditionally
        # is equivalent to matching on the intent- prefix.
        raise FileExistsError(EEXIST, "claim exists")

    mocker.patch("filelock._strict.os.link", side_effect=collide_intent, autospec=True)
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

    mocker.patch("filelock._strict.os.link", side_effect=fail_held, autospec=True)
    lock = StrictSoftFileLock(lock_path)

    with pytest.raises(SoftFileLockProtocolError, match="hard-link publication"):
        lock.acquire()
    assert (lock.claims, lock.is_locked) == ((), False)


def test_strict_soft_held_collision_does_not_delete_foreign_claim(tmp_path: Path, mocker: MockerFixture) -> None:
    lock_path = tmp_path / "resource.lock"
    _initialize_protocol(lock_path)
    token = "f" * 32
    real_link = os.link

    def publish_foreign_held(
        source: _PathValue,
        destination: _PathValue,
        *,
        src_dir_fd: int | None = None,
        dst_dir_fd: int | None = None,
        follow_symlinks: bool = True,
    ) -> None:
        if Path(os.fsdecode(destination)).name.startswith("held-"):
            _write_foreign_claim(Path(f"{lock_path}.filelock") / "claims", token)
            raise FileExistsError(EEXIST, "foreign claim won")
        real_link(
            source,
            destination,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
            follow_symlinks=follow_symlinks,
        )

    mocker.patch("filelock._strict.secrets.token_hex", return_value=token, autospec=True)
    mocker.patch("filelock._strict.os.link", side_effect=publish_foreign_held, autospec=True)
    lock = StrictSoftFileLock(lock_path)

    with pytest.raises(FileExistsError, match="foreign claim won"):
        lock.acquire()
    assert ([(claim.state, claim.start) for claim in lock.claims], lock.is_locked) == ([("held", 4242)], False)


def _write_foreign_claim(claims: Path, token: str, state: str = "held") -> Path:
    claim = claims / f"{state}-v1-{token}.claim"
    claim.write_bytes(b"filelock-strict-v1\n" + token.encode() + f"\n{os.getpid()}\n686f7374\n4242\n".encode())
    return claim


@pytest.mark.parametrize("state", ["intent", "held"])
def test_strict_soft_lost_link_reply_counts_as_committed(lock_path: Path, mocker: MockerFixture, state: str) -> None:
    real_link = os.link

    def link_then_report_exists(
        source: _PathValue,
        destination: _PathValue,
        *,
        src_dir_fd: int | None = None,
        dst_dir_fd: int | None = None,
        follow_symlinks: bool = True,
    ) -> None:
        # An NFS retransmit of a LINK whose reply was lost: the first request committed, the retry sees the name.
        real_link(source, destination, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd, follow_symlinks=follow_symlinks)
        if Path(os.fsdecode(destination)).name.startswith(f"{state}-"):
            raise FileExistsError(EEXIST, "retransmitted link")

    mocker.patch("filelock._strict.os.link", autospec=True, side_effect=link_then_report_exists)

    with (lock := StrictSoftFileLock(lock_path, timeout=0)):
        assert (lock.is_locked, sorted(claim.state for claim in lock.claims)) == (True, ["held", "intent"])
    assert lock.claims == ()


@NEEDS_UNLINK_OPEN_FILE  # pragma: needs unlink-open-file
def test_strict_soft_lost_link_reply_survives_a_peer_reaping_the_source(lock_path: Path, mocker: MockerFixture) -> None:
    real_link: Final = os.link

    def link_then_peer_reaps(
        source: _PathValue,
        destination: _PathValue,
        *,
        src_dir_fd: int | None = None,
        dst_dir_fd: int | None = None,
        follow_symlinks: bool = True,
    ) -> None:
        real_link(source, destination, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd, follow_symlinks=follow_symlinks)
        if Path(os.fsdecode(destination)).name.startswith("intent-"):
            # A contender's scan reaps the private record as soon as its link count reaches two, before the
            # retransmitted LINK answers EEXIST.
            Path(f"{lock_path}.filelock", "claims", os.fsdecode(source)).unlink()
            raise FileExistsError(EEXIST, "retransmitted link")

    mocker.patch("filelock._strict.os.link", autospec=True, side_effect=link_then_peer_reaps)

    with (lock := StrictSoftFileLock(lock_path, timeout=0)):
        assert (lock.is_locked, sorted(claim.state for claim in lock.claims)) == (True, ["held", "intent"])
    assert lock.claims == ()


@pytest.mark.parametrize("scan_to_empty", [pytest.param(2, id="intent"), pytest.param(3, id="held")])
def test_strict_soft_force_break_during_doorway_backs_off(
    tmp_path: Path, mocker: MockerFixture, scan_to_empty: int
) -> None:
    lock_path = tmp_path / "resource.lock"
    _initialize_protocol(lock_path)
    real_scandir = os.scandir
    scans = 0

    def break_before_scan(path: int | str) -> Iterator[os.DirEntry[str]]:
        nonlocal scans
        scans += 1
        if scans == scan_to_empty:
            for claim in Path(f"{lock_path}.filelock", "claims").glob("*.claim"):
                claim.unlink()
        return real_scandir(path)

    mocker.patch("filelock._strict.os.scandir", side_effect=break_before_scan, autospec=True)
    lock = StrictSoftFileLock(lock_path, timeout=0)

    with pytest.raises(Timeout):
        lock.acquire()
    assert (lock.claims, lock.is_locked) == ((), False)


def test_strict_soft_force_break_held_during_doorway_backs_off(tmp_path: Path, mocker: MockerFixture) -> None:
    lock_path = tmp_path / "resource.lock"
    _initialize_protocol(lock_path)
    real_scandir = os.scandir
    scans = 0

    def break_held_before_final_scan(path: int | str) -> Iterator[os.DirEntry[str]]:
        nonlocal scans
        scans += 1
        if scans == 3:
            for claim in Path(f"{lock_path}.filelock", "claims").glob("held-*.claim"):
                claim.unlink()
        return real_scandir(path)

    mocker.patch("filelock._strict.os.scandir", side_effect=break_held_before_final_scan, autospec=True)
    lock = StrictSoftFileLock(lock_path, timeout=0)

    with pytest.raises(Timeout):
        lock.acquire()
    assert (lock.claims, lock.is_locked) == ((), False)


@_NEEDS_DIR_FD  # pragma: needs dir-fd
def test_strict_soft_reaper_removes_private_before_publication(tmp_path: Path, mocker: MockerFixture) -> None:
    lock_path = tmp_path / "resource.lock"
    _initialize_protocol(lock_path)
    token = "0" * 32
    private_path = _private_claim_path(lock_path, token)
    _reap_before_publication(lock_path, token, mocker)
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

    mocker.patch("filelock._strict.secrets.token_hex", return_value=token, autospec=True)
    mocker.patch("filelock._strict.os.link", side_effect=reap_after_link, autospec=True)

    with StrictSoftFileLock(lock_path, timeout=0) as lock:
        assert (lock.claims[0].state, private_path.exists()) == ("held", False)


@_NEEDS_DIR_FD  # pragma: needs dir-fd
def test_strict_soft_reaper_replacement_only_aborts_publisher(tmp_path: Path, mocker: MockerFixture) -> None:
    lock_path = tmp_path / "resource.lock"
    _initialize_protocol(lock_path)
    token = "0" * 32
    private_path = _private_claim_path(lock_path, token)
    displaced_path = private_path.with_suffix(".displaced")
    real_unlink = os.unlink
    replaced = False

    def replace_instead_of_unlink(path: _PathValue, *, dir_fd: int | None = None) -> None:
        # The reaper's clock probe and the peer's own records are the other os.unlink callers; let them through.
        nonlocal replaced
        if os.fsdecode(path) != private_path.name:
            real_unlink(path, dir_fd=dir_fd)
            return
        replaced = True
        private_path.replace(displaced_path)
        private_path.write_bytes(b"replacement publisher")

    _reap_before_publication(lock_path, token, mocker)
    mocker.patch("filelock._strict.os.unlink", autospec=True, side_effect=replace_instead_of_unlink)
    lock = StrictSoftFileLock(lock_path, timeout=0)

    with pytest.raises(Timeout):
        lock.acquire()
    assert (
        replaced,
        lock.is_locked,
        lock.claims,
        (private_path.parent / f"intent-v1-{token}.claim").exists(),
        private_path.read_bytes(),
        displaced_path.exists(),
    ) == (
        True,
        False,
        (),
        False,
        b"replacement publisher",
        True,
    )


def _reap_before_publication(lock_path: Path, token: str, mocker: MockerFixture) -> None:  # pragma: needs dir-fd
    real_link: Final = os.link
    peer_scanned = False

    def reap_before_link(
        source: _PathValue,
        destination: _PathValue,
        *,
        src_dir_fd: int | None = None,
        dst_dir_fd: int | None = None,
        follow_symlinks: bool = True,
    ) -> None:
        nonlocal peer_scanned
        # A peer's scan ages out the private record before this publisher links it; the peer's own links pass.
        if Path(os.fsdecode(destination)).name.startswith("intent-") and not peer_scanned:
            peer_scanned = True
            os.utime(_private_claim_path(lock_path, token), (0, 0))
            with StrictSoftFileLock(lock_path, timeout=0):
                pass
        real_link(source, destination, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd, follow_symlinks=follow_symlinks)

    real_token_hex: Final = secrets.token_hex
    mocker.patch(
        "filelock._strict.secrets.token_hex",
        autospec=True,
        side_effect=lambda nbytes: real_token_hex(nbytes) if peer_scanned else token,
    )
    mocker.patch("filelock._strict.os.link", autospec=True, side_effect=reap_before_link)


def test_strict_soft_private_close_failure_removes_public_claim(tmp_path: Path, mocker: MockerFixture) -> None:
    lock_path = tmp_path / "resource.lock"
    _initialize_protocol(lock_path)
    real_close = os.close
    real_open_relative = filelock._strict._open_relative
    private_fd: int | None = None

    def capture_private_record(directory_ref: tuple[str, int | None], name: str, flags: int, mode: int) -> int:
        nonlocal private_fd
        fd = real_open_relative(directory_ref, name, flags, mode)
        if flags & os.O_CREAT:
            private_fd = fd
        return fd

    # filelock._strict.os is the os module, so this patches os.close process-wide. Key the fault to the descriptor
    # under test rather than to call order: a background thread closing first would otherwise consume it, which is how
    # a soft-rw heartbeat still winding down turned these injections red on GraalPy.
    def fail_private_close(fd: int) -> None:
        nonlocal private_fd
        real_close(fd)
        if fd == private_fd:
            private_fd = None  # the cleanup that follows opens a directory under the same number
            raise OSError(EIO, "private close failed")

    mocker.patch("filelock._strict._open_relative", side_effect=capture_private_record, autospec=True)
    mocker.patch("filelock._strict.os.close", side_effect=fail_private_close, autospec=True)
    lock = StrictSoftFileLock(lock_path)

    with pytest.raises(OSError, match="private close failed"):
        lock.acquire()
    assert (lock.claims, lock.is_locked) == ((), False)


@_NEEDS_DIR_FD  # pragma: needs dir-fd
def test_strict_soft_link_and_directory_close_failures_preserve_both(tmp_path: Path, mocker: MockerFixture) -> None:
    lock_path = tmp_path / "resource.lock"
    _initialize_protocol(lock_path)
    real_close = os.close
    real_fstat = os.fstat
    link_failed = False
    directory_close_failed = False

    def fail_link(_source: _PathValue, _destination: _PathValue, **_options: int | bool | None) -> None:
        nonlocal link_failed
        link_failed = True
        raise OSError(EIO, "link failed")

    def fail_directory_close(fd: int) -> None:
        nonlocal directory_close_failed
        is_directory = stat.S_ISDIR(real_fstat(fd).st_mode)
        real_close(fd)
        if is_directory and link_failed and not directory_close_failed:
            directory_close_failed = True
            raise OSError(EIO, "directory close failed")

    mocker.patch("filelock._strict.os.link", side_effect=fail_link, autospec=True)
    mocker.patch("filelock._strict.os.close", side_effect=fail_directory_close, autospec=True)
    lock = StrictSoftFileLock(lock_path)

    with pytest.raises(BaseExceptionGroup) as raised:
        lock.acquire()
    assert ([str(error) for error in raised.value.exceptions], lock.claims, lock.is_locked) == (
        ["[Errno 5] link failed", "[Errno 5] directory close failed"],
        (),
        False,
    )


@_NEEDS_DIR_FD  # pragma: needs dir-fd
@_NEEDS_LINK_DIR_FD  # pragma: needs link-dir-fd
def test_strict_soft_held_directory_close_failure_removes_both_claims(tmp_path: Path, mocker: MockerFixture) -> None:
    lock_path = tmp_path / "resource.lock"
    _initialize_protocol(lock_path)
    _fail_directory_close_after(mocker, "link", "held-", "held directory close failed")
    lock = StrictSoftFileLock(lock_path)

    with pytest.raises(OSError, match="held directory close failed"):
        lock.acquire()
    assert (lock.claims, lock.is_locked) == ((), False)


def _stage_claim_record(lock_path: Path) -> Path:
    _initialize_protocol(lock_path)
    return _private_claim_path(lock_path, "0" * 32)


def _stage_sentinel_record(lock_path: Path) -> Path:
    return lock_path.parent / f".{lock_path.name}{_PRIVATE_RECORD_MARKER}{'0' * 32}.tmp"


_NON_REGULAR_NODES: Final[list[pytest.ParameterSet]] = [
    pytest.param(Path.mkdir, Path.is_dir, id="directory"),
    pytest.param(
        lambda path: path.symlink_to(path.parent / "missing"),
        Path.is_symlink,
        id="symlink",
        marks=pytest.mark.skipif(not CAPABILITIES["symlink"], reason="stages the private record as a symlink"),
    ),
]


@pytest.mark.parametrize(("make_node", "is_node"), _NON_REGULAR_NODES)
def test_strict_soft_reaper_leaves_non_regular_private_node(
    tmp_path: Path, make_node: Callable[[Path], object], is_node: Callable[[Path], bool]
) -> None:
    lock_path = tmp_path / "resource.lock"
    _initialize_protocol(lock_path)
    private_path = _private_claim_path(lock_path, "0" * 32)
    make_node(private_path)  # pragma: needs symlink

    with pytest.raises(SoftFileLockProtocolError, match="cannot list claim directory"):
        _ = StrictSoftFileLock(lock_path).claims
    assert is_node(private_path)


@pytest.mark.parametrize(("make_node", "is_node"), _NON_REGULAR_NODES)
def test_strict_soft_non_regular_sentinel_private_node_fails_closed(
    tmp_path: Path, make_node: Callable[[Path], object], is_node: Callable[[Path], bool]
) -> None:
    lock_path = tmp_path / "resource.lock"
    private_path = tmp_path / f".{lock_path.name}{_PRIVATE_RECORD_MARKER}{'0' * 32}.tmp"
    make_node(private_path)  # pragma: needs symlink

    with pytest.raises(SoftFileLockProtocolError, match="cannot reclaim sentinel private records"):
        StrictSoftFileLock(lock_path, timeout=0).acquire()
    assert is_node(private_path)


def test_strict_soft_reaper_does_not_retry_sharing_error(
    abandoned_record: tuple[Path, Path], mocker: MockerFixture
) -> None:
    lock_path, private_path = abandoned_record
    real_unlink: Final = os.unlink

    def deny_abandoned_record(path: _PathValue, *, dir_fd: int | None = None) -> None:
        if Path(os.fsdecode(path)).name == private_path.name:
            raise PermissionError(EACCES, "sharing violation")
        real_unlink(path, dir_fd=dir_fd)

    unlink = mocker.patch("filelock._strict.os.unlink", autospec=True, side_effect=deny_abandoned_record)
    sleep: Final = mocker.create_autospec(time.sleep)
    _patch_strict_clock(mocker, sleep=sleep)
    # A contender blocked by a foreign claim ages the record out in its scan without removing a claim of its own.
    _write_foreign_claim(Path(f"{lock_path}.filelock") / "claims", "f" * 32)

    with pytest.raises(Timeout):
        StrictSoftFileLock(lock_path, timeout=0).acquire()
    reaper_unlinks = [Path(os.fsdecode(call.args[0])).name for call in unlink.call_args_list].count(private_path.name)
    assert (reaper_unlinks, sleep.call_count, private_path.exists()) == (1, 0, True)


@pytest.mark.parametrize(
    "stage_record",
    [pytest.param(_stage_claim_record, id="claim"), pytest.param(_stage_sentinel_record, id="sentinel")],
)
def test_strict_soft_reaper_ages_records_by_filesystem_clock(
    tmp_path: Path, mocker: MockerFixture, stage_record: Callable[[Path], Path]
) -> None:
    lock_path = tmp_path / "resource.lock"
    private_path = stage_record(lock_path)
    private_path.write_bytes(b"in-flight publication")
    # A client clock an hour ahead of the filesystem's would read the fresh record as abandoned.
    _patch_strict_clock(mocker, wall=lambda: time.time() + 3600)

    with StrictSoftFileLock(lock_path, timeout=0):
        assert private_path.read_bytes() == b"in-flight publication"


def test_strict_soft_reaper_without_filesystem_clock_keeps_records(
    abandoned_record: tuple[Path, Path], mocker: MockerFixture
) -> None:
    lock_path, private_path = abandoned_record
    real_open: Final = os.open

    def deny_clock_probe(path: _PathValue, flags: int, mode: int = 0o777, *, dir_fd: int | None = None) -> int:
        if Path(os.fsdecode(path)).name.startswith(".held-"):
            raise PermissionError(EACCES, "read-only observer")
        return real_open(path, flags, mode, dir_fd=dir_fd)

    mocker.patch("filelock._strict.os.open", autospec=True, side_effect=deny_clock_probe)
    # A contender blocked by a foreign claim scans with the clock probe without publishing a claim of its own.
    _write_foreign_claim(Path(f"{lock_path}.filelock") / "claims", "f" * 32)

    with pytest.raises(Timeout):
        StrictSoftFileLock(lock_path, timeout=0).acquire()
    assert private_path.exists()


def test_strict_soft_claims_inspection_creates_no_file(
    abandoned_record: tuple[Path, Path], mocker: MockerFixture
) -> None:
    lock_path, private_path = abandoned_record
    opened: Final = mocker.spy(os, "open")

    assert StrictSoftFileLock(lock_path).claims == ()
    assert ([call.args for call in opened.call_args_list if call.args[1] & os.O_CREAT], private_path.exists()) == (
        [],
        True,
    )


def _fail_probe_stamp(mocker: MockerFixture) -> None:
    real_write: Final = os.write

    def enospc_on_probe(fd: int, data: bytes) -> int:
        if data == b"\n":
            raise OSError(ENOSPC, "No space left on device")
        return real_write(fd, data)

    mocker.patch("os.write", autospec=True, side_effect=enospc_on_probe)


def _fail_probe_removal(mocker: MockerFixture) -> None:
    real_unlink: Final = os.unlink

    def eio_on_probe(path: _PathValue, *, dir_fd: int | None = None) -> None:
        if Path(os.fsdecode(path)).name.startswith(".held-v1-"):
            raise OSError(EIO, "probe unlink failed")
        real_unlink(path, dir_fd=dir_fd)

    mocker.patch("filelock._strict.os.unlink", autospec=True, side_effect=eio_on_probe)


# An unstamped probe reads no time, so the record's age is untrusted; a probe left behind still read the time.
@pytest.mark.parametrize(
    ("fail_probe", "kept"),
    [pytest.param(_fail_probe_stamp, True, id="stamp"), pytest.param(_fail_probe_removal, False, id="remove")],
)
def test_strict_soft_failed_clock_probe_does_not_fail_the_scan(
    abandoned_record: tuple[Path, Path],
    mocker: MockerFixture,
    fail_probe: Callable[[MockerFixture], None],
    *,
    kept: bool,
) -> None:
    lock_path, private_path = abandoned_record
    fail_probe(mocker)

    with StrictSoftFileLock(lock_path, timeout=0):
        assert private_path.exists() is kept


@_NEEDS_DIR_FD  # pragma: needs dir-fd
def test_strict_soft_release_error_keeps_remaining_claim(tmp_path: Path, mocker: MockerFixture) -> None:
    lock = StrictSoftFileLock(tmp_path / "resource.lock")
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


@_NEEDS_DIR_FD  # pragma: needs dir-fd
def test_strict_soft_release_commits_before_directory_close_error(tmp_path: Path, mocker: MockerFixture) -> None:
    lock_path = tmp_path / "resource.lock"
    lock = StrictSoftFileLock(lock_path)
    lock.acquire()
    _fail_directory_close_after(mocker, "unlink", "held-", "directory close failed")

    with pytest.raises(OSError, match="directory close failed"):
        lock.release()
    mocker.stopall()
    assert (lock.is_locked, lock.lock_counter, lock.claims) == (False, 0, ())
    with StrictSoftFileLock(lock_path, timeout=0) as contender:
        assert contender.is_locked


@_NEEDS_DIR_FD  # pragma: needs dir-fd
def test_strict_soft_release_preserves_unlink_and_directory_close_errors(tmp_path: Path, mocker: MockerFixture) -> None:
    lock = StrictSoftFileLock(tmp_path / "resource.lock")
    lock.acquire()
    real_close = os.close
    real_fstat = os.fstat
    real_unlink = os.unlink
    unlink_failed = False
    directory_close_failed = False

    def fail_claim_unlink(path: _PathValue, *, dir_fd: int | None = None) -> None:
        nonlocal unlink_failed
        if Path(os.fsdecode(path)).name.startswith("held-"):
            unlink_failed = True
            raise PermissionError(EACCES, "claim unlink failed")
        real_unlink(path, dir_fd=dir_fd)

    def fail_directory_close(fd: int) -> None:
        nonlocal directory_close_failed
        is_directory = stat.S_ISDIR(real_fstat(fd).st_mode)
        real_close(fd)
        if is_directory and unlink_failed and not directory_close_failed:
            directory_close_failed = True
            raise OSError(EIO, "directory close failed")

    unlink_mock = mocker.patch("filelock._strict.os.unlink", side_effect=fail_claim_unlink, autospec=True)
    supports_dir_fd_mock = mocker.patch.object(os, "supports_dir_fd", os.supports_dir_fd | {unlink_mock})
    close_mock = mocker.patch("filelock._strict.os.close", side_effect=fail_directory_close, autospec=True)

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


@_NEEDS_DIR_FD  # pragma: needs dir-fd
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
    cleanup_name: str | None = None
    competitor_inserted = False

    def add_competitor_before_final_scan(path: int | str) -> Iterator[os.DirEntry[str]]:
        nonlocal competitor_inserted
        claim_directory = Path(f"{lock_path}.filelock", "claims")
        if not competitor_inserted and (claim_directory / f"held-v1-{owner_token}.claim").exists():
            competitor_inserted = True
            (claim_directory / competitor_name).write_text(
                f"filelock-strict-v1\n{competitor_token}\n{os.getpid()}\n{socket.gethostname().encode().hex()}\n4242\n",
                encoding="ascii",
                newline="",
            )
            return real_scandir(claim_directory)
        return real_scandir(path)

    def fail_held_unlink(path: _PathValue, *, dir_fd: int | None = None) -> None:
        nonlocal cleanup_name
        cleanup_name = Path(os.fsdecode(path)).name
        if cleanup_name == f"held-v1-{owner_token}.claim":
            raise PermissionError(EACCES, "held unlink failed")
        real_unlink(path, dir_fd=dir_fd)

    def fail_cleanup_directory_close(fd: int) -> None:
        nonlocal cleanup_name
        is_directory = stat.S_ISDIR(real_fstat(fd).st_mode)
        real_close(fd)
        if not is_directory:
            return
        # Only the first directory closed after an unlink is the one it ran in; the next walk closes others.
        unlinked, cleanup_name = cleanup_name, None
        if unlinked == f"intent-v1-{owner_token}.claim":
            raise OSError(EIO, "intent directory close failed")
        if unlinked == f"held-v1-{owner_token}.claim":
            raise OSError(EIO, "held directory close failed")

    mocker.patch("filelock._strict.secrets.token_hex", return_value=owner_token, autospec=True)
    mocker.patch("filelock._strict.os.scandir", side_effect=add_competitor_before_final_scan, autospec=True)
    unlink_mock = mocker.patch("filelock._strict.os.unlink", side_effect=fail_held_unlink, autospec=True)
    supports_dir_fd_mock = mocker.patch.object(os, "supports_dir_fd", os.supports_dir_fd | {unlink_mock})
    close_mock = mocker.patch("filelock._strict.os.close", side_effect=fail_cleanup_directory_close, autospec=True)
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
            "[Errno 13] held unlink failed",
            "[Errno 5] held directory close failed",
            "[Errno 5] intent directory close failed",
        ],
        [competitor_name, f"held-v1-{owner_token}.claim"],
        False,
    )
    lock.force_break(competitor_name)
    with lock:
        assert len(lock.claims) == 2


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

    mocker.patch("filelock._strict.os.link", side_effect=publish_competitor, autospec=True)
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

    def reject_publication(source: _PathValue, _destination: _PathValue, **_options: int | bool | None) -> None:
        private_names.append(Path(os.fsdecode(source)).name)
        raise OSError(EIO, "publication failed")

    mocker.patch("filelock._strict.secrets.token_hex", side_effect=["0" * 32, "1" * 32], autospec=True)
    mocker.patch("filelock._strict.os.link", side_effect=reject_publication, autospec=True)

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
    _patch_strict_clock(mocker, monotonic=mocker.create_autospec(time.monotonic, side_effect=[0.0, 2.0]))
    lock = StrictSoftFileLock(lock_path, poll_interval=0)

    with pytest.raises(SoftFileLockProtocolError, match="marker it can parse, unchanged for 2 seconds"):
        lock.acquire()
    assert lock_path.read_bytes() == content


def test_strict_soft_unreadable_lock_path_blocks(tmp_path: Path, mocker: MockerFixture) -> None:
    lock_path = tmp_path / "resource.lock"
    lock_path.write_bytes(b"")
    real_open = os.open

    def sharing_violation(path: _PathValue, flags: int, mode: int = 0o777, *, dir_fd: int | None = None) -> int:
        # Windows refuses to open a marker its legacy holder keeps open.
        if Path(os.fsdecode(path)).name == lock_path.name:
            raise PermissionError(EACCES, "sharing violation")
        return real_open(path, flags, mode, dir_fd=dir_fd)  # pragma: no cover  # an occupied path publishes nothing

    _route_claim_opens_through_os_open(mocker)
    mocker.patch("filelock._strict.os.open", autospec=True, side_effect=sharing_violation)
    lock = StrictSoftFileLock(lock_path, timeout=0)

    with pytest.raises(Timeout):
        lock.acquire()
    assert lock.lock_path_occupant == "a file it cannot read (sharing violation)"


def _route_claim_opens_through_os_open(mocker: MockerFixture) -> None:
    # Windows opens a claim through NtCreateFile so its owner can still unlink it, which an os.open fault would miss.
    if sys.platform == "win32":  # pragma: win32 cover
        mocker.patch(
            "filelock._strict._open_deletable_for_reading",
            side_effect=lambda path: os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0)),
            autospec=True,
        )


def test_strict_soft_exited_soft_lock_owner_raises_without_waiting(tmp_path: Path, mocker: MockerFixture) -> None:
    lock_path = tmp_path / "resource.lock"
    pid = subprocess.run(
        [sys.executable, "-c", "import os; print(os.getpid())"], check=True, capture_output=True, text=True
    ).stdout.strip()
    lock_path.write_text(f"{pid}\n{host_name()}\n", encoding="ascii")
    found = f"a soft-lock marker left by pid {pid} on {host_name()}, which has exited"
    sleep: Final = mocker.create_autospec(time.sleep)
    _patch_strict_clock(mocker, sleep=sleep)
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


def test_strict_soft_lock_path_occupant_is_none_for_the_sentinel(tmp_path: Path) -> None:
    lock = StrictSoftFileLock(tmp_path / "resource.lock")
    before = lock.lock_path_occupant
    with lock:
        assert (before, lock.lock_path_occupant) == (None, None)


def test_strict_soft_sentinel_removed_after_publication_backs_off(tmp_path: Path, mocker: MockerFixture) -> None:
    lock_path = tmp_path / "resource.lock"
    real_publish = filelock._strict._publish_record

    def legacy_break_lock_after_publication(public_path: str, record: bytes, mode: int) -> BaseException | None:
        # With timeout=0 the acquire gives up before any claim, so the sentinel is the only record published.
        cleanup_error = real_publish(public_path, record, mode)
        Path(public_path).unlink()
        return cleanup_error

    mocker.patch("filelock._strict._publish_record", autospec=True, side_effect=legacy_break_lock_after_publication)
    lock = StrictSoftFileLock(lock_path, timeout=0)

    with pytest.raises(Timeout):
        lock.acquire()
    assert (lock.is_locked, lock_path.exists()) == (False, False)


def test_strict_soft_sentinel_vanishing_before_open_is_republished(lock_path: Path, mocker: MockerFixture) -> None:
    real_open = os.open
    vanished = False

    def vanish_once(path: _PathValue, flags: int, mode: int = 0o777, *, dir_fd: int | None = None) -> int:
        # A legacy break_lock() removes the sentinel between the inspection's lstat and its open.
        nonlocal vanished
        if not vanished and Path(os.fsdecode(path)).name == lock_path.name:
            vanished = True
            raise FileNotFoundError(ENOENT, "sentinel removed", os.fsdecode(path))
        return real_open(path, flags, mode, dir_fd=dir_fd)

    _route_claim_opens_through_os_open(mocker)
    mocker.patch("filelock._strict.os.open", autospec=True, side_effect=vanish_once)

    with StrictSoftFileLock(lock_path, timeout=0) as lock:
        assert (vanished, lock.is_locked) == (True, True)


def test_strict_soft_rides_out_a_transient_non_sentinel(tmp_path: Path, mocker: MockerFixture) -> None:
    lock_path = tmp_path / "resource.lock"
    lock_path.mkdir()
    _patch_strict_clock(
        mocker, sleep=mocker.create_autospec(time.sleep, side_effect=lambda _seconds: lock_path.rmdir())
    )

    with StrictSoftFileLock(lock_path, timeout=0) as lock:
        assert lock.is_locked


def test_strict_soft_sentinel_race_with_non_regular_winner_blocks(tmp_path: Path, mocker: MockerFixture) -> None:
    lock_path = tmp_path / "resource.lock"

    def publish_directory(
        _source: _PathValue,
        _destination: _PathValue,
        **_options: int | bool | None,
    ) -> None:
        lock_path.mkdir()
        raise FileExistsError(EEXIST, "sentinel exists")

    mocker.patch("filelock._strict.os.link", side_effect=publish_directory, autospec=True)

    with pytest.raises(SoftFileLockProtocolError, match="found a directory"):
        StrictSoftFileLock(lock_path, timeout=0).acquire()
    assert lock_path.is_dir()


@_NEEDS_DIR_FD  # pragma: needs dir-fd
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

    mocker.patch("filelock._strict.os.unlink", side_effect=fail_first_private_cleanup, autospec=True)

    with pytest.raises(OSError, match="private cleanup failed"):
        StrictSoftFileLock(lock_path).acquire()
    with pytest.raises(Timeout):
        SoftFileLock(lock_path, timeout=0).acquire()
    with StrictSoftFileLock(lock_path, timeout=0) as strict:
        assert (lock_path.read_bytes(), strict.is_locked) == (b"1\nfilelock-strict-v1\x00\n0\n", True)


def test_strict_soft_claim_directory_read_error_fails_closed(tmp_path: Path, mocker: MockerFixture) -> None:
    lock_path = tmp_path / "resource.lock"
    _initialize_protocol(lock_path)
    mocker.patch("filelock._strict.os.scandir", side_effect=PermissionError(EACCES, "cannot list"), autospec=True)

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
    "make_claim",
    [
        pytest.param(Path.mkdir, id="non-regular"),
        pytest.param(lambda path: path.write_bytes(b"x" * 1025), id="oversized"),
    ],
)
def test_strict_soft_invalid_record_node_fails_closed(tmp_path: Path, make_claim: Callable[[Path], object]) -> None:
    lock_path = tmp_path / "resource.lock"
    claims = Path(f"{lock_path}.filelock") / "claims"
    claims.mkdir(parents=True)
    make_claim(claims / f"held-v1-{'0' * 32}.claim")

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
        if not replaced and Path(os.fsdecode(path)).name == claim.name:
            replaced = True
            claim.replace(claim.with_name(f".{claim.name}.replaced"))
            if replacement == "file":
                claim.write_bytes(record)
            else:
                claim.mkdir()
        return real_open(path, flags, mode, dir_fd=dir_fd)

    _route_claim_opens_through_os_open(mocker)
    mocker.patch("filelock._strict.os.open", side_effect=replace_before_open, autospec=True)

    with pytest.raises(SoftFileLockProtocolError, match=message):
        StrictSoftFileLock(lock_path).acquire()


def _write_held_claim(claims: Path) -> Path:
    claims.mkdir(parents=True)
    claim = claims / f"held-v1-{'0' * 32}.claim"
    claim.write_bytes(b"filelock-strict-v1\n" + b"0" * 32 + b"\n1\n686f7374\n\n")
    return claim


def test_strict_soft_stale_claim_read_is_skipped(tmp_path: Path, mocker: MockerFixture) -> None:
    # An NFS peer that unlinks its own claim leaves this client's cached filehandle stale, so the open returns ESTALE
    # rather than ENOENT. A stale handle that outlives revalidation is a vanished claim, not an unreadable one.
    lock_path = tmp_path / "resource.lock"
    claim = _write_held_claim(Path(f"{lock_path}.filelock") / "claims")
    mocker.patch("filelock._strict._CLAIM_READ_GRACE", 0.02)
    _open_raising_for(claim, OSError(ESTALE, "Stale file handle"), mocker)

    assert StrictSoftFileLock(lock_path).claims == ()


def _open_raising_for(claim: Path, error: OSError, mocker: MockerFixture) -> MockType:
    real_open = os.open

    def failing_open(path: _PathValue, flags: int, mode: int = 0o777, *, dir_fd: int | None = None) -> int:
        if Path(os.fsdecode(path)).name == claim.name:
            raise error
        # The flows using this helper read only the single target claim, so only the directory walk passes through.
        return real_open(path, flags, mode, dir_fd=dir_fd)  # pragma: needs dir-fd

    _route_claim_opens_through_os_open(mocker)
    return mocker.patch("filelock._strict.os.open", side_effect=failing_open, autospec=True)


def test_strict_soft_stale_cached_claim_is_skipped(tmp_path: Path, mocker: MockerFixture) -> None:
    lock_path = tmp_path / "resource.lock"
    claim = _write_held_claim(Path(f"{lock_path}.filelock") / "claims")
    mocker.patch("filelock._strict.os.link", autospec=True, side_effect=OSError(ESTALE, "Stale file handle"))

    assert filelock._strict._read_existing_claims(str(lock_path), claim.parent, verify_server=True) == ()


def test_strict_soft_live_claim_survives_server_probe(tmp_path: Path) -> None:
    lock_path = tmp_path / "resource.lock"
    claim = _write_held_claim(Path(f"{lock_path}.filelock") / "claims")

    found = filelock._strict._read_existing_claims(str(lock_path), claim.parent, verify_server=True)

    assert [item.name for item in found] == [claim.name]
    assert [item.name for item in claim.parent.iterdir()] == [claim.name]


def test_strict_soft_claim_probe_error_fails_closed(tmp_path: Path, mocker: MockerFixture) -> None:
    lock_path = tmp_path / "resource.lock"
    claim = _write_held_claim(Path(f"{lock_path}.filelock") / "claims")
    mocker.patch("filelock._strict.os.link", autospec=True, side_effect=OSError(EIO, "I/O error"))

    with pytest.raises(SoftFileLockProtocolError, match="cannot verify claim"):
        filelock._strict._read_existing_claims(str(lock_path), claim.parent, verify_server=True)


def test_strict_soft_claim_probe_cleanup_error_fails_closed(tmp_path: Path, mocker: MockerFixture) -> None:
    lock_path = tmp_path / "resource.lock"
    claim = _write_held_claim(Path(f"{lock_path}.filelock") / "claims")
    mocker.patch("filelock._strict.os.unlink", autospec=True, side_effect=OSError(EIO, "I/O error"))

    with pytest.raises(SoftFileLockProtocolError, match="cannot remove claim probe"):
        filelock._strict._read_existing_claims(str(lock_path), claim.parent, verify_server=True)


def test_strict_soft_claim_probe_removed_by_peer(tmp_path: Path, mocker: MockerFixture) -> None:
    lock_path = tmp_path / "resource.lock"
    claim = _write_held_claim(Path(f"{lock_path}.filelock") / "claims")
    real_unlink: Final = os.unlink

    def remove_before_cleanup(path: _PathValue, *, dir_fd: int | None = None) -> NoReturn:
        real_unlink(path, dir_fd=dir_fd)
        raise FileNotFoundError(ENOENT, "already removed")

    mocker.patch("filelock._strict.os.unlink", autospec=True, side_effect=remove_before_cleanup)

    assert [
        item.name for item in filelock._strict._read_existing_claims(str(lock_path), claim.parent, verify_server=True)
    ] == [claim.name]


def test_strict_soft_claim_probe_requires_hard_links(tmp_path: Path, mocker: MockerFixture) -> None:
    lock_path = tmp_path / "resource.lock"
    claim = _write_held_claim(Path(f"{lock_path}.filelock") / "claims")
    mocker.patch("filelock._strict.os.link", autospec=True, side_effect=NotImplementedError)

    with pytest.raises(SoftFileLockProtocolError, match="hard links unavailable"):
        filelock._strict._read_existing_claims(str(lock_path), claim.parent, verify_server=True)


def test_strict_soft_abandoned_claim_probe_is_reclaimed(tmp_path: Path) -> None:
    lock_path = tmp_path / "resource.lock"
    claim_directory = Path(f"{lock_path}.filelock") / "claims"
    claim_directory.mkdir(parents=True)
    probe = claim_directory / f".probe-v1-{'0' * 32}.tmp"
    probe.write_bytes(b"abandoned")
    os.utime(probe, (time.time() - 10, time.time() - 10))

    with StrictSoftFileLock(lock_path, timeout=0):
        assert not probe.exists()


def test_strict_soft_stale_claim_read_revalidates_to_gone(tmp_path: Path, mocker: MockerFixture) -> None:
    lock_path = tmp_path / "resource.lock"
    claim = _write_held_claim(Path(f"{lock_path}.filelock") / "claims")
    real_open = os.open
    stale_raised = False

    def stale_then_vanish(path: _PathValue, flags: int, mode: int = 0o777, *, dir_fd: int | None = None) -> int:
        nonlocal stale_raised
        if not stale_raised and Path(os.fsdecode(path)).name == claim.name:
            stale_raised = True
            claim.unlink()
            raise OSError(ESTALE, "Stale file handle")
        # The retry re-lstats the vanished claim and fails there before reopening, so only the directory walk passes.
        return real_open(path, flags, mode, dir_fd=dir_fd)  # pragma: needs dir-fd

    _route_claim_opens_through_os_open(mocker)
    mocker.patch("filelock._strict.os.open", side_effect=stale_then_vanish, autospec=True)

    assert StrictSoftFileLock(lock_path).claims == ()
    assert stale_raised


def test_strict_soft_io_error_claim_read_fails_closed(tmp_path: Path, mocker: MockerFixture) -> None:
    lock_path = tmp_path / "resource.lock"
    _open_raising_for(_write_held_claim(Path(f"{lock_path}.filelock") / "claims"), OSError(EIO, "I/O error"), mocker)

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


@_NEEDS_DIR_FD  # pragma: needs dir-fd
@NEEDS_SYMLINK  # pragma: needs symlink
@pytest.mark.parametrize(
    ("function", "suffix", "occurrence", "match"),
    [
        pytest.param("mkdir", "claims", 1, "cannot open claim directory", id="before-creating-claims"),
        pytest.param("open", ".filelock", 1, "cannot open .*filelock$", id="before-settling-the-mode"),
        pytest.param("open", ".filelock", 2, "cannot open .*filelock$", id="before-opening-coordination"),
    ],
)
def test_strict_soft_coordination_directory_swapped_for_symlink_leaves_target_alone(
    tmp_path: Path, mocker: MockerFixture, function: str, suffix: str, occurrence: int, match: str
) -> None:
    lock_path: Final = tmp_path / "resource.lock"
    (coordination := Path(f"{lock_path}.filelock")).mkdir()
    (target := tmp_path / "target").mkdir()
    real: Final = getattr(os, function)
    matches = 0

    def swap_at_the_occurrence(path: _PathValue, *args: int, **options: int | None) -> int | None:
        # A process that can rename entries in the lock's directory can swap the name after the lock saw a directory.
        nonlocal matches
        matches += os.fsdecode(path).endswith(suffix)
        if matches == occurrence and not coordination.is_symlink():
            coordination.rename(tmp_path / "aside")
            coordination.symlink_to(target, target_is_directory=True)
        return real(path, *args, **options)

    mocker.patch(f"filelock._strict.os.{function}", autospec=True, side_effect=swap_at_the_occurrence)

    with pytest.raises(SoftFileLockProtocolError, match=match):
        StrictSoftFileLock(lock_path, timeout=0).acquire()
    assert list(target.iterdir()) == []


@_NEEDS_DIR_FD  # pragma: needs dir-fd
@NEEDS_SYMLINK  # pragma: needs symlink
def test_strict_soft_release_keeps_claims_while_coordination_directory_is_a_symlink(
    lock_path: Path, tmp_path: Path
) -> None:
    coordination, aside, target = Path(f"{lock_path}.filelock"), tmp_path / "aside", tmp_path / "target"
    (target / "claims").mkdir(parents=True)
    lock = StrictSoftFileLock(lock_path)
    lock.acquire()
    coordination.rename(aside)
    coordination.symlink_to(target, target_is_directory=True)

    # Following the symlink would find no claim in the target, count both as removed and strand them.
    with pytest.raises(BaseExceptionGroup, match="strict claim release failed"):
        lock.release()
    assert (lock.is_locked, len(list((aside / "claims").iterdir()))) == (True, 2)

    coordination.unlink()
    aside.rename(coordination)
    lock.release()
    assert (lock.is_locked, lock.claims) == (False, ())


@_NEEDS_DIR_FD  # pragma: needs dir-fd
@NEEDS_SYMLINK  # pragma: needs symlink
def test_strict_soft_symlinked_claims_directory_is_neither_listed_nor_broken(tmp_path: Path) -> None:
    lock_path = tmp_path / "resource.lock"
    claim = _write_held_claim(tmp_path / "target" / "claims")
    Path(f"{lock_path}.filelock").mkdir()
    Path(f"{lock_path}.filelock", "claims").symlink_to(claim.parent, target_is_directory=True)
    lock = StrictSoftFileLock(lock_path)

    with pytest.raises(SoftFileLockProtocolError, match="cannot open claim directory"):
        _ = lock.claims
    with pytest.raises(OSError, match=re.escape("claims'")):
        lock.force_break(claim.name)
    assert claim.exists()


@_NEEDS_DIR_FD  # pragma: needs dir-fd
@NEEDS_SYMLINK  # pragma: needs symlink
def test_strict_soft_symlinked_protocol_directory_is_neither_listed_nor_broken(tmp_path: Path) -> None:
    lock_path = tmp_path / "resource.lock"
    claim = _write_held_claim(tmp_path / "target" / "claims")
    Path(f"{lock_path}.filelock").symlink_to(claim.parent.parent, target_is_directory=True)
    lock = StrictSoftFileLock(lock_path)

    with pytest.raises(SoftFileLockProtocolError, match="cannot open claim directory"):
        _ = lock.claims
    with pytest.raises(OSError, match=re.escape("resource.lock.filelock'")):
        lock.force_break(claim.name)
    assert claim.exists()


@NEEDS_FOREIGN_STICKY_TMP
@pytest.mark.usefixtures("directories_of_another_account")
def test_strict_soft_refuses_coordination_directory_of_another_account(  # pragma: needs foreign-sticky-tmp
    lock_path_in_shared_tmp: Path,
) -> None:
    (coordination := Path(f"{lock_path_in_shared_tmp}.filelock")).mkdir()

    with pytest.raises(SoftFileLockProtocolError, match="not to this process's account"):
        StrictSoftFileLock(lock_path_in_shared_tmp, timeout=0).acquire()
    assert list(coordination.iterdir()) == []


def test_strict_soft_coordination_directory_replaced_in_a_writable_parent_splits_holders(tmp_path: Path) -> None:
    # The documented directory boundary: a process that can rename entries in a non-sticky parent can move even this
    # account's own protocol directory aside and start a fresh one, which the lock cannot tell from a first use.
    lock_path: Final = tmp_path / "resource.lock"
    (first := StrictSoftFileLock(lock_path)).acquire()
    Path(f"{lock_path}.filelock").rename(tmp_path / "aside")
    (second := StrictSoftFileLock(lock_path, timeout=0)).acquire()

    assert (first.is_locked, second.is_locked) == (True, True)


@pytest.fixture
def lock_path(tmp_path: Path) -> Path:
    _initialize_protocol(path := tmp_path / "resource.lock")
    return path


@pytest.fixture
def abandoned_record(lock_path: Path) -> tuple[Path, Path]:
    (private_path := _private_claim_path(lock_path, "0" * 32)).write_bytes(b"abandoned")
    os.utime(private_path, (0, 0))
    return lock_path, private_path


def _initialize_protocol(lock_path: Path) -> None:
    with StrictSoftFileLock(lock_path):
        pass


def _private_claim_path(lock_path: Path, token: str) -> Path:
    return Path(f"{lock_path}.filelock") / "claims" / f".intent-v1-{token}.claim.private-v1-{token}.tmp"


def _fail_directory_close_after(  # pragma: needs dir-fd
    mocker: MockerFixture, function: str, prefix: str, message: str
) -> None:
    # Every claim operation walks to its directory through descriptors it closes on the way, so fail the first
    # directory close after os.link or os.unlink touched the name rather than count the directory closes. Where os.link
    # takes no dir_fd it gets full paths, so match the final component.
    real_close: Final = os.close
    real_fstat: Final = os.fstat
    real_function: Final = getattr(os, function)
    armed = False

    def arm_after(*paths: _PathValue, **options: int | bool | None) -> None:
        nonlocal armed
        real_function(*paths, **options)
        armed = armed or Path(os.fsdecode(paths[-1])).name.startswith(prefix)

    def fail_directory_close(fd: int) -> None:
        nonlocal armed
        is_directory = stat.S_ISDIR(real_fstat(fd).st_mode)
        real_close(fd)
        if armed and is_directory:
            armed = False
            raise OSError(EIO, message)

    mocker.patch(f"filelock._strict.os.{function}", side_effect=arm_after, autospec=True)
    mocker.patch("filelock._strict.os.close", side_effect=fail_directory_close, autospec=True)


def _leaf_error_messages(error: BaseException) -> list[str]:  # pragma: needs dir-fd
    if isinstance(error, BaseExceptionGroup):
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
    mocker.patch("filelock._strict.os.link", side_effect=error, autospec=True)

    assert _probe_link_follow_symlinks() is False


def test_strict_soft_sentinel_inspection_and_close_failure_group(tmp_path: Path, mocker: MockerFixture) -> None:
    lock_path = tmp_path / "resource.lock"
    _initialize_protocol(lock_path)
    real_fstat = os.fstat
    real_close = os.close
    real_open_sentinel = filelock._strict._open_sentinel
    sentinel_fd: int | None = None

    def capture_sentinel(lock_file: str, path: Path, unchanged_for: Callable[[os.stat_result], float]) -> int | None:
        nonlocal sentinel_fd
        sentinel_fd = real_open_sentinel(lock_file, path, unchanged_for)
        return sentinel_fd

    # The record read inside _open_sentinel stats the same descriptor, but it runs before the capture above, so
    # keying on the descriptor still leaves _acquire's own inspection as the first stat to fail.
    def fail_sentinel_inspection(fd: int) -> os.stat_result:
        if fd == sentinel_fd:
            raise OSError(EIO, "sentinel inspection failed")
        return real_fstat(fd)

    def fail_sentinel_close(fd: int) -> None:
        if fd == sentinel_fd:
            raise OSError(EIO, "sentinel close failed")
        # Besides the sentinel this flow closes only the descriptor the lock creates the claim directory against.
        real_close(fd)  # pragma: needs dir-fd

    mocker.patch("filelock._strict._open_sentinel", side_effect=capture_sentinel, autospec=True)
    mocker.patch("filelock._strict.os.fstat", side_effect=fail_sentinel_inspection, autospec=True)
    mocker.patch("filelock._strict.os.close", side_effect=fail_sentinel_close, autospec=True)
    lock = StrictSoftFileLock(lock_path)

    with pytest.raises(BaseExceptionGroup) as raised:
        lock.acquire()
    assert ([str(error) for error in raised.value.exceptions], lock.is_locked) == (
        ["[Errno 5] sentinel inspection failed", "[Errno 5] sentinel close failed"],
        False,
    )


@_NEEDS_DIR_FD  # pragma: needs dir-fd
@pytest.mark.parametrize(
    ("initialize", "published"),
    [
        pytest.param(lambda _lock_path: None, "resource.lock", id="sentinel"),
        pytest.param(_initialize_protocol, "intent-", id="intent"),
    ],
)
def test_strict_soft_publication_directory_close_failure(
    tmp_path: Path, mocker: MockerFixture, initialize: Callable[[Path], None], published: str
) -> None:
    lock_path = tmp_path / "resource.lock"
    initialize(lock_path)
    _fail_directory_close_after(mocker, "link", published, "publication directory close failed")
    lock = StrictSoftFileLock(lock_path, timeout=0)

    with pytest.raises(OSError, match="publication directory close failed"):
        lock.acquire()
    assert lock.is_locked is False


@_NEEDS_DIR_FD  # pragma: needs dir-fd
def test_strict_soft_force_break_directory_close_failure(tmp_path: Path, mocker: MockerFixture) -> None:
    lock = StrictSoftFileLock(tmp_path / "resource.lock")
    lock.acquire()
    claim_name = lock.claims[0].name
    _fail_directory_close_after(mocker, "unlink", claim_name, "force break directory close failed")

    with pytest.raises(OSError, match="force break directory close failed"):
        lock.force_break(claim_name)
    lock.release()
    assert (lock.is_locked, lock.claims) == (False, ())


def test_strict_soft_release_sentinel_close_failure(tmp_path: Path, mocker: MockerFixture) -> None:
    lock = StrictSoftFileLock(tmp_path / "resource.lock")
    lock.acquire()
    sentinel_fd = lock._context.lock_file_fd
    real_close = os.close

    def fail_sentinel_close(fd: int) -> None:
        if fd == sentinel_fd:
            raise OSError(EIO, "sentinel close failed")
        # Only posix release closes a second, non-sentinel descriptor (the dir_fd); win32 closes only the sentinel.
        real_close(fd)  # pragma: needs dir-fd

    mocker.patch("filelock._strict.os.close", side_effect=fail_sentinel_close, autospec=True)

    with pytest.raises(OSError, match="sentinel close failed"):
        lock.release()
    assert (lock.is_locked, lock.claims) == (False, ())


def test_strict_soft_discard_sentinel_close_failure(tmp_path: Path, mocker: MockerFixture) -> None:
    lock_path = tmp_path / "resource.lock"
    _initialize_protocol(lock_path)
    _write_foreign_claim(Path(f"{lock_path}.filelock") / "claims", "a" * 32)
    real_close = os.close
    real_fstat = os.fstat
    sentinel = lock_path.stat()

    def fail_sentinel_close(fd: int) -> None:
        if os.path.samestat(real_fstat(fd), sentinel):
            raise OSError(EIO, "doorway sentinel close failed")
        real_close(fd)

    mocker.patch("filelock._strict.os.close", side_effect=fail_sentinel_close, autospec=True)
    lock = StrictSoftFileLock(lock_path)

    with pytest.raises(OSError, match="doorway sentinel close failed"):
        lock.acquire()
    assert (lock.is_locked, [claim.state for claim in lock.claims]) == (False, ["held"])


@pytest.mark.parametrize(
    "initialize",
    [
        pytest.param(lambda _lock_path: None, id="sentinel"),
        pytest.param(_initialize_protocol, id="intent"),
    ],
)
def test_strict_soft_publication_record_replaced_aborts(
    tmp_path: Path, mocker: MockerFixture, initialize: Callable[[Path], None]
) -> None:
    lock_path = tmp_path / "resource.lock"
    initialize(lock_path)
    real_identity = _relative_identity

    def mismatch_public(directory_ref: tuple[str, int | None], name: str) -> tuple[int, int] | None:
        identity = real_identity(directory_ref, name)
        if identity is not None and not name.startswith("."):
            return identity[0], identity[1] + 1
        return identity

    mocker.patch("filelock._strict._relative_identity", side_effect=mismatch_public, autospec=True)
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
        # Only the dot-prefixed private record is inspected in this flow, so this passthrough never runs.
        return real_identity(directory_ref, name)  # pragma: no cover

    mocker.patch("filelock._strict._relative_identity", side_effect=source_gone, autospec=True)
    mocker.patch(
        "filelock._strict._link_relative", side_effect=FileNotFoundError(ENOENT, "source vanished"), autospec=True
    )
    lock = StrictSoftFileLock(lock_path, timeout=0)

    with pytest.raises(Timeout):
        lock.acquire()
    assert lock.is_locked is False


def test_strict_soft_private_inspection_failure_unlinks_vanished_record(tmp_path: Path, mocker: MockerFixture) -> None:
    lock_path = tmp_path / "resource.lock"
    _initialize_protocol(lock_path)
    real_fstat = os.fstat
    real_open_relative = filelock._strict._open_relative
    private_fd: int | None = None

    def capture_private_record(directory_ref: tuple[str, int | None], name: str, flags: int, mode: int) -> int:
        nonlocal private_fd
        fd = real_open_relative(directory_ref, name, flags, mode)
        if flags & os.O_CREAT:
            private_fd = fd
        return fd

    def fail_private_inspection(fd: int) -> os.stat_result:
        if fd == private_fd:
            raise OSError(EIO, "private inspection failed")
        return real_fstat(fd)

    mocker.patch("filelock._strict._open_relative", side_effect=capture_private_record, autospec=True)
    mocker.patch("filelock._strict.os.fstat", side_effect=fail_private_inspection, autospec=True)
    mocker.patch(
        "filelock._strict._unlink_relative", side_effect=FileNotFoundError(ENOENT, "record gone"), autospec=True
    )
    lock = StrictSoftFileLock(lock_path)

    with pytest.raises(OSError, match="private inspection failed"):
        lock.acquire()
    assert lock.is_locked is False


def test_strict_soft_private_record_reclaimed_after_close_is_tolerated(tmp_path: Path, mocker: MockerFixture) -> None:
    lock_path = tmp_path / "resource.lock"
    _initialize_protocol(lock_path)
    real_close = os.close
    real_open_relative = filelock._strict._open_relative
    private_records: dict[int, Path] = {}

    def capture_private_record(directory_ref: tuple[str, int | None], name: str, flags: int, mode: int) -> int:
        fd = real_open_relative(directory_ref, name, flags, mode)
        if flags & os.O_CREAT:
            private_records[fd] = Path(directory_ref[0], name)
        return fd

    def reclaim_on_close(fd: int) -> None:
        real_close(fd)
        if (record := private_records.pop(fd, None)) is not None:
            record.unlink()

    mocker.patch("filelock._strict._open_relative", side_effect=capture_private_record, autospec=True)
    mocker.patch("filelock._strict.os.close", side_effect=reclaim_on_close, autospec=True)
    lock = StrictSoftFileLock(lock_path, timeout=0)

    lock.acquire()
    assert lock.is_locked is True
    lock.release()
    assert lock.is_locked is False


def test_strict_soft_record_finalization_close_and_unlink_failures_group(tmp_path: Path, mocker: MockerFixture) -> None:
    lock_path = tmp_path / "resource.lock"
    _initialize_protocol(lock_path)
    real_close = os.close
    real_open_relative = filelock._strict._open_relative
    private_fd: int | None = None

    def capture_private_record(directory_ref: tuple[str, int | None], name: str, flags: int, mode: int) -> int:
        nonlocal private_fd
        fd = real_open_relative(directory_ref, name, flags, mode)
        if flags & os.O_CREAT:
            private_fd = fd
        return fd

    def fail_private_close(fd: int) -> None:
        nonlocal private_fd
        real_close(fd)
        if fd == private_fd:
            private_fd = None  # the cleanup that follows opens a directory under the same number
            raise OSError(EIO, "private close failed")

    mocker.patch("filelock._strict._open_relative", side_effect=capture_private_record, autospec=True)
    mocker.patch("filelock._strict.os.close", side_effect=fail_private_close, autospec=True)
    mocker.patch(
        "filelock._strict._unlink_relative", side_effect=PermissionError(EACCES, "unlink denied"), autospec=True
    )
    lock = StrictSoftFileLock(lock_path)

    with pytest.raises(BaseExceptionGroup) as raised:
        lock.acquire()
    assert (sorted(str(error) for error in raised.value.exceptions), lock.is_locked) == (
        ["[Errno 13] unlink denied", "[Errno 5] private close failed"],
        False,
    )


def test_strict_soft_release_claim_unlink_failures_group(tmp_path: Path, mocker: MockerFixture) -> None:
    lock = StrictSoftFileLock(tmp_path / "resource.lock")
    lock.acquire()
    unlink_mock = mocker.patch(
        "filelock._strict._unlink_in_directory",
        side_effect=PermissionError(EACCES, "claim unlink denied"),
        autospec=True,
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


def test_strict_soft_doorway_claim_unlink_failure_stays_pending(tmp_path: Path, mocker: MockerFixture) -> None:
    lock_path = tmp_path / "resource.lock"
    _initialize_protocol(lock_path)
    owner_token = "f" * 32
    competitor_name = f"held-v1-{'0' * 32}.claim"
    intent_name = f"intent-v1-{owner_token}.claim"
    real_scandir = os.scandir
    injected = False

    def add_competitor_once_intent_is_published(path: int | str) -> Iterator[os.DirEntry[str]]:
        # Key on directory content, not scandir call count: the setup and publication paths scan a differing number
        # of times per platform, so plant the held competitor on the first rescan that already lists our own intent.
        nonlocal injected
        if not injected:
            with real_scandir(path) as entries:
                intent_published = any(entry.name == intent_name for entry in entries)
            if intent_published:
                _write_foreign_claim(Path(f"{lock_path}.filelock") / "claims", "0" * 32)
                injected = True
        return real_scandir(path)

    real_unlink_in_directory = filelock._strict._unlink_in_directory

    def deny_only_the_owned_intent_unlink(directory: Path, name: str) -> BaseException | None:
        # Fail only the doorway's own intent-claim unlink. A blanket failure also breaks the Windows-only private
        # record cleanup inside publication (_unlink_relative unlinks a differently named private file there), firing
        # the fault before the doorway ever discards and masking the commit-owned path this test targets.
        if name == intent_name:
            raise PermissionError(EACCES, "intent unlink denied")
        return real_unlink_in_directory(directory, name)  # pragma: lacks dir-fd

    mocker.patch("filelock._strict.secrets.token_hex", return_value=owner_token, autospec=True)
    scandir_mock = mocker.patch(
        "filelock._strict.os.scandir", side_effect=add_competitor_once_intent_is_published, autospec=True
    )
    unlink_mock = mocker.patch(
        "filelock._strict._unlink_in_directory", side_effect=deny_only_the_owned_intent_unlink, autospec=True
    )
    lock = StrictSoftFileLock(lock_path)

    with pytest.raises(PermissionError, match="intent unlink denied"):
        lock.acquire()
    assert (lock.is_locked, sorted(claim.name for claim in lock.claims)) == (False, [competitor_name, intent_name])
    # The leftover intent is a pending cleanup, not a hold: retrying acquires nothing while the competitor holds.
    with pytest.raises(PermissionError, match="intent unlink denied"):
        lock.acquire(timeout=0)
    mocker.stop(scandir_mock)
    mocker.stop(unlink_mock)
    with pytest.raises(Timeout):
        lock.acquire(timeout=0)
    assert ([claim.name for claim in lock.claims], lock.is_locked) == ([competitor_name], False)
    lock.force_break(competitor_name)


def _leave_pending_doorway_here(  # pragma: needs collected-finalization
    lock: StrictSoftFileLock, mocker: MockerFixture
) -> None:
    _fail_the_doorway(mocker)
    with pytest.raises(PermissionError, match="intent unlink denied"):
        lock.acquire()
    mocker.stopall()
    assert [claim.state for claim in lock.claims] == ["intent"]


def _leave_pending_doorway_in_exited_thread(lock: StrictSoftFileLock, mocker: MockerFixture) -> None:
    (linger := threading.Event()).set()
    _leave_pending_doorway(lock, mocker, linger=linger).join()


@NEEDS_COLLECTED_FINALIZATION
@pytest.mark.parametrize(
    ("leave", "thread_local"),
    [
        pytest.param(_leave_pending_doorway_here, True, id="this-thread"),
        pytest.param(_leave_pending_doorway_in_exited_thread, True, id="exited-thread"),
        pytest.param(_leave_pending_doorway_here, False, id="shared-context"),
    ],
)
def test_strict_soft_dropped_instance_removes_pending_claims(  # pragma: needs collected-finalization
    lock_path: Path,
    mocker: MockerFixture,
    leave: Callable[[StrictSoftFileLock, MockerFixture], None],
    thread_local: bool,
) -> None:
    lock = StrictSoftFileLock(lock_path, thread_local=thread_local)
    leave(lock, mocker)

    del lock
    gc.collect()

    assert StrictSoftFileLock(lock_path).claims == ()


@_NEEDS_FD_DIRECTORY  # pragma: needs fd-directory
def test_strict_soft_failed_doorway_cleanup_keeps_no_descriptor(lock_path: Path, mocker: MockerFixture) -> None:
    real_link: Final = os.link

    def fail_held_link(source: _PathValue, destination: _PathValue, **options: int | bool | None) -> None:
        if Path(os.fsdecode(destination)).name.startswith("held-"):
            raise OSError(EIO, "held link failed")
        real_link(source, destination, **options)  # ty: ignore[invalid-argument-type]  # the forwarded options are wider than os.link keyword types

    mocker.patch("filelock._strict.os.link", autospec=True, side_effect=fail_held_link)
    _deny_claim_unlinks(mocker)
    lock: Final = StrictSoftFileLock(lock_path)

    with pytest.raises(BaseExceptionGroup, match="strict doorway claim cleanup failed"):
        lock.acquire()
    assert (descriptors_on(lock_path.stat()), lock.is_locked) == (0, False)


@_NEEDS_FD_DIRECTORY  # pragma: needs fd-directory
@NEEDS_COLLECTED_FINALIZATION
@pytest.mark.parametrize("thread_local", [pytest.param(True, id="thread-local"), pytest.param(False, id="shared")])
def test_strict_soft_dropped_hold_with_stuck_claims_closes_its_descriptor(  # pragma: needs collected-finalization
    lock_path: Path, mocker: MockerFixture, thread_local: bool
) -> None:
    lock = StrictSoftFileLock(lock_path, thread_local=thread_local)
    lock.acquire()
    _deny_claim_unlinks(mocker)

    del lock
    gc.collect()

    assert descriptors_on(lock_path.stat()) == 0


def _deny_claim_unlinks(mocker: MockerFixture) -> None:  # pragma: needs fd-directory
    real_unlink: Final = os.unlink

    def deny(path: _PathValue, *, dir_fd: int | None = None) -> None:
        if os.fsdecode(path).endswith(".claim"):
            raise PermissionError(EACCES, "claim unlink denied")
        real_unlink(path, dir_fd=dir_fd)

    mocker.patch("filelock._strict.os.unlink", autospec=True, side_effect=deny)


def test_strict_soft_acquire_leaves_a_live_threads_pending_doorway_alone(
    lock_path: Path, mocker: MockerFixture
) -> None:
    lock: Final = StrictSoftFileLock(lock_path, timeout=0)
    linger: Final = threading.Event()
    worker: Final = _leave_pending_doorway(lock, mocker, linger=linger)
    try:
        # The worker finishes its own doorway on its next acquire; finishing it here would race that.
        with pytest.raises(Timeout):
            lock.acquire()
        assert [claim.state for claim in lock.claims] == ["intent"]
    finally:
        linger.set()
        worker.join()


def test_strict_soft_acquire_finishes_an_exited_threads_pending_doorway(lock_path: Path, mocker: MockerFixture) -> None:
    lock: Final = StrictSoftFileLock(lock_path, timeout=0)
    _leave_pending_doorway_in_exited_thread(lock, mocker)

    with lock:
        assert [claim.state for claim in lock.claims] == ["held", "intent"]
    assert lock.claims == ()


def _leave_pending_doorway(
    lock: StrictSoftFileLock, mocker: MockerFixture, *, linger: threading.Event
) -> threading.Thread:
    _fail_the_doorway(mocker)
    failed: Final = threading.Event()

    def acquire_then_linger() -> None:
        with pytest.raises(PermissionError, match="intent unlink denied"):
            lock.acquire()
        failed.set()
        linger.wait()

    (worker := threading.Thread(target=acquire_then_linger)).start()
    failed.wait()
    mocker.stopall()
    assert [claim.state for claim in lock.claims] == ["intent"]
    return worker


def _fail_the_doorway(mocker: MockerFixture) -> None:
    real_unlink_in_directory: Final = filelock._strict._unlink_in_directory

    def deny_intent_unlink(directory: Path, name: str) -> BaseException | None:
        if name.startswith("intent-"):
            raise PermissionError(EACCES, "intent unlink denied")
        return real_unlink_in_directory(directory, name)

    def fail_held_link(_directory: Path, _source: str, _destination: str) -> NoReturn:
        # Raise a fresh error each call: a shared side_effect instance would keep its traceback, and with it the lock.
        raise OSError(EIO, "held link failed")

    mocker.patch("filelock._strict._link_no_replace", autospec=True, side_effect=fail_held_link)
    mocker.patch("filelock._strict._unlink_in_directory", autospec=True, side_effect=deny_intent_unlink)


def test_strict_soft_denied_claim_removed_during_grace_is_skipped(tmp_path: Path, mocker: MockerFixture) -> None:
    lock_path: Final = tmp_path / "resource.lock"
    claim: Final = _write_held_claim(Path(f"{lock_path}.filelock") / "claims")
    clock: Final = iter([0, 1])

    def tick() -> int:
        # Remove the claim as the grace runs out, after the last denied open, so only the listing recheck sees it go.
        if now := next(clock):
            claim.unlink()
        return now

    _patch_strict_clock(
        mocker,
        monotonic=mocker.create_autospec(time.monotonic, side_effect=tick),
        sleep=mocker.create_autospec(time.sleep),
    )
    _open_raising_for(claim, PermissionError(EACCES, "sharing violation"), mocker)

    assert StrictSoftFileLock(lock_path).claims == ()


def test_strict_soft_denied_claim_still_listed_after_grace_fails_closed(tmp_path: Path, mocker: MockerFixture) -> None:
    lock_path: Final = tmp_path / "resource.lock"
    claim: Final = _write_held_claim(Path(f"{lock_path}.filelock") / "claims")
    clock: Final = iter([0, 1])
    _patch_strict_clock(
        mocker,
        monotonic=mocker.create_autospec(time.monotonic, side_effect=lambda: next(clock)),
        sleep=mocker.create_autospec(time.sleep),
    )
    _open_raising_for(claim, PermissionError(EACCES, "sharing violation"), mocker)

    with pytest.raises(SoftFileLockProtocolError, match="cannot read claim"):
        _ = StrictSoftFileLock(lock_path).claims


@pytest.mark.skipif(sys.platform == "win32", reason="Windows has no directory mode to settle")
def test_strict_soft_directory_mode_failure_fails_closed(  # pragma: win32 no cover
    tmp_path: Path, mocker: MockerFixture
) -> None:
    mocker.patch("filelock._strict.os.fchmod", autospec=True, side_effect=PermissionError(EACCES, "read-only mount"))

    with pytest.raises(SoftFileLockProtocolError, match="cannot set the mode"):
        StrictSoftFileLock(tmp_path / "resource.lock").acquire()


def test_strict_soft_claims_removed_one_by_one_share_one_read_grace(tmp_path: Path, mocker: MockerFixture) -> None:
    lock_path: Final = tmp_path / "resource.lock"
    (claims := Path(f"{lock_path}.filelock") / "claims").mkdir(parents=True)
    denied: Final = [_write_foreign_claim(claims, f"{index:032x}") for index in range(10)]
    now = 0.0

    def tick(seconds: float) -> None:
        # Another process removes the lowest unreadable claim every 0.2 s, inside the grace a single claim gets.
        nonlocal now
        before, now = now, now + seconds
        if int(now / 0.2) > int(before / 0.2) and (remaining := [claim for claim in denied if claim.exists()]):
            remaining[0].unlink()

    real_open: Final = os.open

    def deny_claims(path: _PathValue, flags: int, mode: int = 0o777, *, dir_fd: int | None = None) -> int:
        if os.fsdecode(path).endswith(".claim"):
            raise PermissionError(EACCES, "claim read denied")
        return real_open(path, flags, mode, dir_fd=dir_fd)  # pragma: needs dir-fd

    _patch_strict_clock(
        mocker,
        monotonic=mocker.create_autospec(time.monotonic, side_effect=lambda: now),
        sleep=mocker.create_autospec(time.sleep, side_effect=tick),
    )
    _route_claim_opens_through_os_open(mocker)
    mocker.patch("filelock._strict.os.open", autospec=True, side_effect=deny_claims)

    with pytest.raises(SoftFileLockProtocolError, match="cannot read claim"):
        _ = StrictSoftFileLock(lock_path).claims
    assert now < 1


@_WINDOWS_ONLY  # pragma: win32 cover
def test_strict_soft_claim_pending_deletion_is_skipped(tmp_path: Path) -> None:
    # A peer's unlink sets the claim's delete disposition, then closes its handle. Until that close, every open fails
    # with EACCES, and a peer stalled in between can hold that past the read grace, yet the claim is already withdrawn.
    # The marker skips the run off Windows; the assert narrows the platform for ty.
    assert sys.platform == "win32"
    import ctypes

    lock_path = tmp_path / "resource.lock"
    claim = _write_held_claim(Path(f"{lock_path}.filelock") / "claims")
    kernel32 = ctypes.WinDLL("kernel32")  # a private instance, so the restype below stays local
    kernel32.CreateFileW.restype = ctypes.c_void_p
    delete_access, share_all, open_existing, file_disposition_info = 0x10000, 0x1 | 0x2 | 0x4, 3, 4
    handle = ctypes.c_void_p(kernel32.CreateFileW(str(claim), delete_access, share_all, None, open_existing, 0, None))
    delete_file = ctypes.c_bool(True)  # the FILE_DISPOSITION_INFO BOOLEAN
    try:
        assert kernel32.SetFileInformationByHandle(
            handle, file_disposition_info, ctypes.byref(delete_file), ctypes.sizeof(delete_file)
        )
        assert StrictSoftFileLock(lock_path).claims == ()
    finally:
        kernel32.CloseHandle(handle)


def test_strict_soft_claim_removed_between_inspection_and_open_is_skipped(
    tmp_path: Path, mocker: MockerFixture
) -> None:
    claim: Final = _write_held_claim(Path(f"{tmp_path / 'resource.lock'}.filelock") / "claims")

    def remove_after_inspection(inspect: _Inspect) -> _Inspect:
        def inspect_then_remove(path: _PathValue, **options: int | bool | None) -> os.stat_result:
            found = inspect(path, **options)
            # Windows inspects nothing else in this scan; with dir_fd the claim directory is stat-ed too.
            if Path(os.fsdecode(path)).name == claim.name:  # pragma: no branch
                claim.unlink()
            return found

        return inspect_then_remove

    # The lock inspects the claim with lstat by path, or with stat relative to its directory where dir_fd works.
    for function in ("lstat", "stat"):
        mocker.patch(
            f"filelock._strict.os.{function}", side_effect=remove_after_inspection(getattr(os, function)), autospec=True
        )

    assert StrictSoftFileLock(tmp_path / "resource.lock").claims == ()


def test_strict_soft_owner_releases_while_a_contender_reads_its_claim(lock_path: Path, mocker: MockerFixture) -> None:
    # On Windows a reader that opened the claim without FILE_SHARE_DELETE made the owner's unlink fail until it closed.
    (holder := StrictSoftFileLock(lock_path)).acquire()
    real_read: Final = os.read

    def release_mid_read(fd: int, length: int) -> bytes:
        # The scan reads this one claim and nothing else, so the holder releases once.
        holder.release()
        return real_read(fd, length)

    mocker.patch("filelock._strict.os.read", autospec=True, side_effect=release_mid_read)
    _ = StrictSoftFileLock(lock_path).claims

    assert (holder.is_locked, StrictSoftFileLock(lock_path).claims) == (False, ())


@_WINDOWS_ONLY  # pragma: win32 cover
@pytest.mark.parametrize(
    ("operation", "match"),
    [
        pytest.param(lambda lock: lock.acquire(), "is not a real directory", id="acquire"),
        pytest.param(lambda lock: lock.claims, "cannot open claim directory", id="claims"),
    ],
)
def test_strict_soft_refuses_a_junction_for_the_claim_directory(
    tmp_path: Path,
    create_junction: Callable[[Path, Path], None],
    operation: Callable[[StrictSoftFileLock], object],
    match: str,
) -> None:
    lock_path: Final = tmp_path / "resource.lock"
    Path(f"{lock_path}.filelock").mkdir()
    (target := tmp_path / "target").mkdir()
    create_junction(Path(f"{lock_path}.filelock", "claims"), target)

    with pytest.raises(SoftFileLockProtocolError, match=match):
        operation(StrictSoftFileLock(lock_path, timeout=0))
    assert list(target.iterdir()) == []


@_WINDOWS_ONLY  # pragma: win32 cover
def test_strict_soft_coordination_directory_swapped_for_a_junction_leaves_target_alone(
    tmp_path: Path, mocker: MockerFixture, create_junction: Callable[[Path, Path], None]
) -> None:
    lock_path: Final = tmp_path / "resource.lock"
    (coordination := Path(f"{lock_path}.filelock")).mkdir()
    (target := tmp_path / "target").mkdir()
    swapped: list[bool] = []

    def swap_after_the_inspection(inspect: _Inspect) -> _Inspect:
        def inspect_then_swap(path: _PathValue, **options: int | bool | None) -> os.stat_result:
            # The name was a real directory at inspection; by the time the lock creates claims in it, it is a junction.
            found = inspect(path, **options)
            if not swapped and os.fsdecode(path) == str(coordination):
                swapped.append(True)
                coordination.rename(tmp_path / "aside")
                create_junction(coordination, target)
            return found

        return inspect_then_swap

    # pathlib inspects the directory with os.lstat on 3.14 and with os.stat(follow_symlinks=False) before that.
    for function in ("lstat", "stat"):
        mocker.patch(
            f"filelock._strict.os.{function}",
            side_effect=swap_after_the_inspection(getattr(os, function)),
            autospec=True,
        )

    with pytest.raises(SoftFileLockProtocolError, match="cannot open"):
        StrictSoftFileLock(lock_path, timeout=0).acquire()
    assert list(target.iterdir()) == []


@_WINDOWS_ONLY  # pragma: win32 cover
def test_strict_soft_keeps_the_claim_directory_in_place_while_it_scans(lock_path: Path, mocker: MockerFixture) -> None:
    # Without dir_fd every operation resolves the claim directory by name, so nothing may rename it meanwhile.
    real_scandir: Final = os.scandir
    aside: Final = lock_path.parent / "aside"
    refused: list[bool] = []

    def try_to_move_it_aside(path: int | str) -> Iterator[os.DirEntry[str]]:
        try:
            Path(f"{lock_path}.filelock").rename(aside)
        except PermissionError:
            refused.append(True)
        return real_scandir(path)

    mocker.patch("filelock._strict.os.scandir", side_effect=try_to_move_it_aside, autospec=True)
    with StrictSoftFileLock(lock_path, timeout=0) as lock:
        assert lock.is_locked

    assert (bool(refused), aside.exists()) == (True, False)


def test_strict_soft_claims_recheck_after_a_denied_claim_creates_no_file(tmp_path: Path, mocker: MockerFixture) -> None:
    lock_path: Final = tmp_path / "resource.lock"
    claim: Final = _write_held_claim(Path(f"{lock_path}.filelock") / "claims")
    (private_path := _private_claim_path(lock_path, "0" * 32)).write_bytes(b"abandoned")
    os.utime(private_path, (0, 0))
    _patch_strict_clock(
        mocker,
        monotonic=mocker.create_autospec(time.monotonic, side_effect=[0, 1]),
        sleep=mocker.create_autospec(time.sleep),
    )
    opened: Final = _open_raising_for(claim, PermissionError(EACCES, "sharing violation"), mocker)

    with pytest.raises(SoftFileLockProtocolError, match="cannot read claim"):
        _ = StrictSoftFileLock(lock_path).claims
    assert ([call.args for call in opened.call_args_list if call.args[1] & os.O_CREAT], private_path.exists()) == (
        [],
        True,
    )


def test_strict_soft_claim_read_retries_after_a_slow_denied_attempt(tmp_path: Path, mocker: MockerFixture) -> None:
    # The grace window opens at the first pending result, so a first attempt that itself outlasts the window still
    # gets a retry rather than failing closed on a transient denial.
    lock_path = tmp_path / "resource.lock"
    claim = _write_held_claim(Path(f"{lock_path}.filelock") / "claims")
    mocker.patch("filelock._strict._CLAIM_READ_GRACE", 0.02)
    real_open = os.open
    denied = False

    def deny_slowly_once(path: _PathValue, flags: int, mode: int = 0o777, *, dir_fd: int | None = None) -> int:
        nonlocal denied
        if not denied and Path(os.fsdecode(path)).name == claim.name:
            denied = True
            time.sleep(0.05)  # simplint: ignore[SMP808]  # needs a clock hook for grace window
            raise PermissionError(EACCES, "sharing violation")
        return real_open(path, flags, mode, dir_fd=dir_fd)

    _route_claim_opens_through_os_open(mocker)
    mocker.patch("filelock._strict.os.open", side_effect=deny_slowly_once, autospec=True)

    assert ([found.name for found in StrictSoftFileLock(lock_path).claims], denied) == ([claim.name], True)


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
    real_fstat = os.fstat

    def fail_close(fd: int) -> None:
        # Reading the single oversized claim closes one record, so failing every such close still fails once.
        is_directory = stat.S_ISDIR(real_fstat(fd).st_mode)
        real_close(fd)
        if not is_directory:  # pragma: needs dir-fd
            raise OSError(EIO, "record close failed")

    mocker.patch("filelock._strict.os.close", side_effect=fail_close, autospec=True)

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
    mocker.patch("filelock._strict._MKDIR_SUPPORTS_DIR_FD", new=False)
    mocker.patch("filelock._strict._SCANDIR_SUPPORTS_FD", new=False)

    with StrictSoftFileLock(lock_path) as lock:
        assert (lock.is_locked, [claim.state for claim in lock.claims]) == (True, ["held", "intent"])
    assert StrictSoftFileLock(lock_path).claims == ()


@_NEEDS_DIR_FD  # pragma: needs dir-fd
@_NEEDS_LINK_DIR_FD  # pragma: needs link-dir-fd
def test_strict_soft_held_link_and_directory_close_failure(tmp_path: Path, mocker: MockerFixture) -> None:
    lock_path = tmp_path / "resource.lock"
    _initialize_protocol(lock_path)
    real_close = os.close
    real_fstat = os.fstat
    real_link = os.link
    held_failed = False
    directory_closed = False

    def fail_held_link(
        source: _PathValue,
        destination: _PathValue,
        *,
        src_dir_fd: int | None = None,
        dst_dir_fd: int | None = None,
        follow_symlinks: bool = True,
    ) -> None:
        nonlocal held_failed
        if Path(os.fsdecode(destination)).name.startswith("held-"):
            held_failed = True
            raise OSError(EIO, "held link failed")
        real_link(source, destination, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd, follow_symlinks=follow_symlinks)

    def fail_directory_close_after_held(fd: int) -> None:
        nonlocal directory_closed
        is_directory = stat.S_ISDIR(real_fstat(fd).st_mode)
        real_close(fd)
        if is_directory and held_failed and not directory_closed:
            directory_closed = True
            raise OSError(EIO, "held link directory close failed")

    mocker.patch("filelock._strict.os.link", side_effect=fail_held_link, autospec=True)
    mocker.patch("filelock._strict.os.close", side_effect=fail_directory_close_after_held, autospec=True)
    lock = StrictSoftFileLock(lock_path)

    with pytest.raises(BaseExceptionGroup) as raised:
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
        return os.stat_result((stat.S_IFIFO | 0o644, *real_fstat(fd)[1:]))

    mocker.patch("filelock._strict.os.fstat", side_effect=non_regular_fstat, autospec=True)

    with pytest.raises(SoftFileLockProtocolError, match="is not a regular file"):
        _ = StrictSoftFileLock(lock_path).claims


@_WINDOWS_ONLY  # pragma: win32 cover
def test_strict_soft_release_rides_out_a_transient_unlink_denial(lock_path: Path, mocker: MockerFixture) -> None:
    # Another process can hold a claim open for a moment after its owner finished with it, so the release retries once.
    (lock := StrictSoftFileLock(lock_path)).acquire()
    real_unlink: Final = os.unlink
    denied: list[str] = []

    def deny_once(path: _PathValue, *, dir_fd: int | None = None) -> None:
        if not denied and os.fsdecode(path).endswith(".claim"):
            denied.append(os.fsdecode(path))
            raise PermissionError(EACCES, "sharing violation")
        real_unlink(path, dir_fd=dir_fd)

    mocker.patch("filelock._strict.os.unlink", side_effect=deny_once, autospec=True)
    sleep: Final = mocker.create_autospec(time.sleep)
    _patch_strict_clock(mocker, sleep=sleep)
    lock.release()

    assert (len(denied), sleep.call_args_list, lock.claims) == (1, [mocker.call(0.001)], ())


def _patch_strict_clock(
    mocker: MockerFixture,
    *,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    wall: Callable[[], float] = time.time,
) -> None:
    mocker.patch("filelock._strict.time", new=SimpleNamespace(monotonic=monotonic, sleep=sleep, time=wall))
