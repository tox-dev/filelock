from __future__ import annotations

import os
import sys
import threading
from errno import EEXIST, ENOENT
from pathlib import Path
from typing import TYPE_CHECKING, Final, NoReturn

import pytest

from filelock import StrictSoftFileLock, Timeout
from filelock._soft._strict._directories import reclaim_private_record
from filelock._soft._strict._publication import unlink_in_directory

if TYPE_CHECKING:
    from collections.abc import Callable
    from contextlib import AbstractContextManager

    from pytest_mock import MockerFixture
from .capability_marks import WINDOWS_ONLY
from .claim_records import write_foreign_claim
from .filesystem_faults import mock_legacy_volume

pytestmark: Final[pytest.MarkDecorator] = pytest.mark.requires_hard_links


@WINDOWS_ONLY  # pragma: win32 cover
@pytest.mark.parametrize(
    "prepare",
    [pytest.param(lambda _: None, id="posix-disposition"), pytest.param(mock_legacy_volume, id="legacy-volume")],
)
def test_strict_soft_release_with_a_shared_reader_removes_claims(
    initialized_lock_path: Path, mocker: MockerFixture, prepare: Callable[[MockerFixture], None]
) -> None:
    assert sys.platform == "win32"
    import filelock._filesystem._windows._oplocks as oplocks

    prepare(mocker)
    lock = StrictSoftFileLock(initialized_lock_path, timeout=0)
    lock.acquire()
    claims = Path(f"{initialized_lock_path}.filelock") / "claims"
    with os.fdopen(oplocks.open_read_without_oplock_wait(str(next(claims.glob("held-*.claim")))), "rb") as reader:
        lock.release()
        names = [path.name for path in claims.glob("*.claim")]
        with StrictSoftFileLock(initialized_lock_path, timeout=0) as successor:
            assert (names, successor.is_locked, bool(reader.read())) == ([], True, True)


@WINDOWS_ONLY  # pragma: win32 cover
@pytest.mark.parametrize(
    ("prepare", "method"),
    [
        pytest.param(
            lambda _: None,
            "filelock._filesystem._windows._handles.try_set_delete_disposition",
            id="posix-disposition",
        ),
        pytest.param(mock_legacy_volume, "filelock._soft._strict._windows.rename_open_file", id="legacy-rename"),
    ],
)
def test_strict_soft_release_waits_for_windows_io(
    initialized_lock_path: Path, mocker: MockerFixture, prepare: Callable[[MockerFixture], None], method: str
) -> None:
    assert sys.platform == "win32"
    from pkgutil import resolve_name

    lock = StrictSoftFileLock(initialized_lock_path, timeout=0)
    lock.acquire()
    prepare(mocker)
    original = resolve_name(method)
    reader, writer = os.pipe()

    def wait_for_peer(handle: int, *names: str, **options: bool) -> bool | None:
        mocker.stop(pause)
        # An empty pipe exercises the same synchronous I/O cancellation as a slow filesystem operation.
        peer = threading.Timer(0.8, os.write, args=(writer, b"x"))
        peer.start()
        try:
            assert os.read(reader, 1) == b"x"
        finally:
            peer.join()
        return original(handle, *names, **options)

    pause = mocker.patch(method, side_effect=wait_for_peer, autospec=True)
    try:
        lock.release()
    finally:
        os.close(writer)
        os.close(reader)
    assert pause.called
    assert (lock.is_locked, lock.claims) == (False, ())


@WINDOWS_ONLY  # pragma: win32 cover
@pytest.mark.parametrize(
    ("failure", "held", "claim_count"),
    [
        pytest.param("rename", True, 2, id="before-withdrawal"),
        pytest.param("private-cleanup", False, 0, id="after-withdrawal"),
    ],
)
def test_strict_soft_windows_legacy_release_reports_errors(
    initialized_lock_path: Path, mocker: MockerFixture, failure: str, *, held: bool, claim_count: int
) -> None:
    lock = StrictSoftFileLock(initialized_lock_path, timeout=0)
    lock.acquire()
    mock_legacy_volume(mocker, failure)

    with pytest.raises(ExceptionGroup) as caught:
        lock.release()
    assert len(caught.value.exceptions) == 2
    assert all(isinstance(error, OSError) and error.strerror == failure for error in caught.value.exceptions)
    assert (lock.is_locked, len(lock.claims)) == (held, claim_count)
    mocker.stopall()
    lock.release()
    with lock:
        assert lock.is_locked


@WINDOWS_ONLY  # pragma: win32 cover
@pytest.mark.parametrize(
    ("target", "share", "flags"),
    [
        pytest.param(lambda claims: claims.parent, 0, 0x02000000, id="directory"),
        pytest.param(lambda claims: next(claims.glob("held-*.claim")), 0x1 | 0x2, 0, id="claim"),
    ],
)
def test_strict_soft_rejects_readers_that_would_block_release(
    initialized_lock_path: Path, target: Callable[[Path], Path], share: int, flags: int
) -> None:
    assert sys.platform == "win32"
    import ctypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.restype = ctypes.c_void_p
    with StrictSoftFileLock(initialized_lock_path) as lock:
        claims = Path(f"{initialized_lock_path}.filelock") / "claims"
        handle = kernel32.CreateFileW(str(target(claims)), 0x80000000, share, None, 3, flags, None)
        assert (handle, ctypes.get_last_error()) == (ctypes.c_void_p(-1).value, 32)

    assert lock.claims == ()
    with StrictSoftFileLock(initialized_lock_path, timeout=0) as successor:
        assert successor.is_locked


@WINDOWS_ONLY  # pragma: win32 cover
@pytest.mark.parametrize(
    ("state", "error_type"),
    [pytest.param("intent", Timeout, id="intent"), pytest.param("held", FileExistsError, id="held")],
)
def test_strict_soft_windows_claim_collision_keeps_peer(
    initialized_lock_path: Path, mocker: MockerFixture, state: str, error_type: type[Exception]
) -> None:
    assert sys.platform == "win32"
    token = "f" * 32
    from filelock._soft._strict._windows import publish_deletable_claim

    real_publish = publish_deletable_claim
    claims = Path(f"{initialized_lock_path}.filelock") / "claims"

    def collide(
        private_path: Callable[[], str],
        public_path: str,
        record: bytes,
        descriptors: dict[str, int],
        deadline: float | None,
    ) -> int:
        if Path(public_path).name.startswith(f"{state}-"):
            write_foreign_claim(claims, token, state)
            raise FileExistsError(EEXIST, "peer claim exists")
        return real_publish(private_path, public_path, record, descriptors, deadline)

    mocker.patch("secrets.token_hex", return_value=token, autospec=True)
    mocker.patch("filelock._soft._strict._lock.publish_deletable_claim", side_effect=collide, autospec=True)
    lock = StrictSoftFileLock(initialized_lock_path, timeout=0)
    with pytest.raises(error_type):
        lock.acquire()
    assert ([(claim.state, claim.start) for claim in lock.claims], lock.is_locked) == ([(state, 4242)], False)


@WINDOWS_ONLY  # pragma: win32 cover
def test_strict_soft_windows_coordination_directory_stays_in_place(initialized_lock_path: Path) -> None:
    assert sys.platform == "win32"
    coordination = Path(f"{initialized_lock_path}.filelock")
    with StrictSoftFileLock(initialized_lock_path), pytest.raises(PermissionError):
        coordination.rename(initialized_lock_path.parent / "aside")
    coordination.rename(initialized_lock_path.parent / "aside")


@WINDOWS_ONLY  # pragma: win32 cover
def test_strict_soft_windows_failed_claim_publication_removes_private(tmp_path: Path) -> None:
    assert sys.platform == "win32"
    from filelock._soft._strict._windows import publish_deletable_claim

    private = tmp_path / "private.claim"
    public = tmp_path / "public.claim"
    public.write_bytes(b"peer")

    with pytest.raises(FileExistsError):
        publish_deletable_claim(lambda: str(private), str(public), b"owner", {})

    assert (private.exists(), public.read_bytes()) == (False, b"peer")


@WINDOWS_ONLY  # pragma: win32 cover
def test_strict_soft_windows_private_name_collision_preserves_peer(tmp_path: Path) -> None:
    assert sys.platform == "win32"
    from filelock._soft._strict._windows import publish_deletable_claim

    private = tmp_path / "private.claim"
    private.write_bytes(b"peer")

    with pytest.raises(FileExistsError):
        publish_deletable_claim(lambda: str(private), str(tmp_path / "public.claim"), b"owner", {})

    assert private.read_bytes() == b"peer"


def test_strict_soft_scan_tolerates_disappearing_private_record(
    initialized_lock_path: Path, mocker: MockerFixture
) -> None:
    private = (
        Path(f"{initialized_lock_path}.filelock") / "claims" / f".held-v1-{'1' * 32}.claim.private-v1-{'2' * 32}.tmp"
    )
    private.write_bytes(b"unpublished")
    reclaim = reclaim_private_record

    def remove_before_inspection(
        directory_ref: tuple[str, int | None], name: str, now: Callable[[], float | None]
    ) -> None:
        assert name == private.name
        private.unlink()
        reclaim(directory_ref, name, now)

    mocker.patch(
        "filelock._soft._strict._scan.reclaim_private_record",
        side_effect=remove_before_inspection,
        autospec=True,
    )
    with StrictSoftFileLock(initialized_lock_path, blocking=False) as lock:
        assert lock.is_locked
    assert not private.exists()


def test_strict_soft_release_accepts_a_claim_already_removed(
    initialized_lock_path: Path, mocker: MockerFixture
) -> None:
    lock = StrictSoftFileLock(initialized_lock_path, timeout=0)
    lock.acquire()
    unlink = unlink_in_directory

    def removed(
        directory: Path,
        name: str,
        *,
        claim_fd: int | None = None,
        cancel_wait: AbstractContextManager[None] | None = None,
    ) -> NoReturn:
        unlink(directory, name, claim_fd=claim_fd, cancel_wait=cancel_wait)
        raise FileNotFoundError(ENOENT, "claim gone")

    removal = mocker.patch(
        "filelock._soft._strict._publication.unlink_in_directory", side_effect=removed, autospec=True
    )
    lock.release()
    mocker.stop(removal)
    assert (lock.is_locked, lock.claims) == (False, ())
    with lock:
        assert lock.is_locked
