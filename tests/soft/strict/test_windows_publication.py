from __future__ import annotations

import functools
import os
import sys
from errno import EACCES, EEXIST, EIO
from pathlib import Path
from typing import TYPE_CHECKING, Final, NoReturn

import pytest

from filelock import StrictSoftFileLock, Timeout

if TYPE_CHECKING:
    import ctypes
    from collections.abc import Callable
    from ctypes import wintypes

    from pytest_mock import MockerFixture
from .capability_marks import WINDOWS_ONLY
from .filesystem_faults import mock_legacy_volume

pytestmark: Final[pytest.MarkDecorator] = pytest.mark.requires_hard_links


@WINDOWS_ONLY  # pragma: win32 cover
@pytest.mark.parametrize("directory", ["resource.lock.filelock", "claims"])
def test_windows_directory_pin_retries_after_oplock_break(  # pragma: win32 cover
    tmp_path: Path, directory: str, mocker: MockerFixture
) -> None:
    assert sys.platform == "win32"

    import filelock._filesystem._windows._directories as directories
    import filelock._filesystem._windows._handles as handles

    lock = StrictSoftFileLock(tmp_path / "resource.lock", blocking=False)
    create = directories.nt_create

    def refuse_pin(
        path: str,
        access: int,
        attributes: int,
        share: int,
        disposition: int,
        options: int = handles.CREATE_OPTIONS,
        *,
        descriptor: ctypes.c_void_p | None = None,
        raw: wintypes.HANDLE | None = None,
    ) -> tuple[int, int]:
        if Path(path).name == directory and options & handles.FILE_COMPLETE_IF_OPLOCKED:
            return 0, handles.STATUS_OPLOCK_BREAK_IN_PROGRESS
        return create(path, access, attributes, share, disposition, options, descriptor=descriptor, raw=raw)

    refusal = mocker.patch.object(directories, "nt_create", side_effect=refuse_pin, autospec=True)
    with pytest.raises(Timeout):
        lock.acquire()
    assert (lock.is_locked, lock.claims) == (False, ())
    mocker.stop(refusal)
    with lock:
        assert lock.is_locked


@WINDOWS_ONLY  # pragma: win32 cover
def test_strict_soft_windows_pin_failure_leaves_no_pending_cleanup(
    initialized_lock_path: Path, mocker: MockerFixture
) -> None:
    assert sys.platform == "win32"
    lock = StrictSoftFileLock(initialized_lock_path, blocking=False)
    failure = mocker.patch(
        "filelock._soft._strict._lock.pin_claim_directory",
        side_effect=PermissionError(EACCES, "cannot pin"),
        autospec=True,
    )
    with pytest.raises(PermissionError, match="cannot pin"):
        lock.acquire()
    failure.assert_called_once_with(Path(f"{initialized_lock_path}.filelock") / "claims", acquiring=True)
    assert lock.claims == ()
    mocker.stop(failure)
    with lock:
        assert lock.is_locked


@WINDOWS_ONLY  # pragma: win32 cover
def test_strict_soft_windows_failed_private_cleanup_reports_both_errors(tmp_path: Path, mocker: MockerFixture) -> None:
    assert sys.platform == "win32"
    from filelock._soft._strict import _windows as strict_windows

    private, public = tmp_path / "private", tmp_path / "public"
    public.write_bytes(b"peer")
    cleanup_error = OSError(EIO, "could not remove private record")
    failure = mocker.patch.object(strict_windows, "mark_claim_for_deletion", side_effect=cleanup_error, autospec=True)
    descriptors: dict[str, int] = {}

    with pytest.raises(ExceptionGroup, match="strict publication cleanup failed") as caught:
        strict_windows.publish_deletable_claim(lambda: str(private), str(public), b"owner", descriptors)

    publication, cleanup = caught.value.exceptions
    assert isinstance(publication, FileExistsError)
    assert cleanup is cleanup_error
    mocker.stop(failure)
    fd = descriptors.pop(str(public))
    try:
        strict_windows.mark_claim_for_deletion(fd, str(private))
    finally:
        os.close(fd)
    assert (private.exists(), public.read_bytes()) == (False, b"peer")


@WINDOWS_ONLY  # pragma: win32 cover
@pytest.mark.parametrize(
    ("prepare", "error_type", "message"),
    [
        pytest.param(lambda _: None, OSError, "could not confirm rename", id="ntfs"),
        pytest.param(mock_legacy_volume, OSError, "could not confirm rename", id="legacy"),
        pytest.param(
            lambda mocker: mock_legacy_volume(mocker, "private-cleanup"),
            ExceptionGroup,
            "strict doorway cleanup failed",
            id="legacy-cleanup-error",
        ),
    ],
)
def test_strict_soft_windows_interrupted_rename_with_a_shared_reader(
    initialized_lock_path: Path,
    mocker: MockerFixture,
    prepare: Callable[[MockerFixture], None],
    error_type: type[Exception],
    message: str,
    request: pytest.FixtureRequest,
) -> None:
    assert sys.platform == "win32"
    import filelock._filesystem._windows._oplocks as oplocks
    from filelock._soft._strict import _windows as strict_windows

    lock = StrictSoftFileLock(initialized_lock_path, timeout=0)
    claims = Path(f"{initialized_lock_path}.filelock") / "claims"
    rename = strict_windows.rename_open_file
    interruption = OSError(EIO, "could not confirm rename")

    def interrupt(fd: int, path: str) -> None:
        rename(fd, path)
        if Path(path).name.startswith("intent-"):
            request.addfinalizer(functools.partial(os.close, oplocks.open_read_without_oplock_wait(path)))
            raise interruption

    prepare(mocker)
    mocker.patch.object(strict_windows, "rename_open_file", side_effect=interrupt, autospec=True)
    with pytest.raises(error_type, match=message):
        lock.acquire()
    assert list(claims.glob("*.claim")) == []
    mocker.stopall()
    with StrictSoftFileLock(initialized_lock_path, timeout=0) as successor:
        assert successor.is_locked


@WINDOWS_ONLY  # pragma: win32 cover
def test_strict_soft_windows_retries_an_interrupted_rename(initialized_lock_path: Path, mocker: MockerFixture) -> None:
    assert sys.platform == "win32"
    import ctypes

    import filelock._filesystem._windows._handles as handles
    from filelock._soft._strict import _windows as strict_windows

    lock = StrictSoftFileLock(initialized_lock_path, timeout=0)
    rename = strict_windows.rename_open_file
    interruption = OSError(EIO, "could not confirm rename")

    def interrupt(fd: int, public_path: str) -> NoReturn:
        rename(fd, public_path)
        raise interruption

    def deny_delete(_handle: int, *, published: bool) -> bool:
        ctypes.set_last_error(5 if published else 32)  # ERROR_ACCESS_DENIED or ERROR_SHARING_VIOLATION
        return False

    mocker.patch.object(strict_windows, "rename_open_file", side_effect=interrupt, autospec=True)
    mocker.patch.object(handles, "try_set_delete_disposition", side_effect=deny_delete, autospec=True)
    with pytest.raises(ExceptionGroup, match="strict doorway claim cleanup failed") as caught:
        lock.acquire()
    publication, rollback = caught.value.exceptions
    assert isinstance(rollback, PermissionError)
    assert isinstance(publication, ExceptionGroup)
    assert publication.exceptions[0] is interruption
    assert isinstance(publication.exceptions[1], PermissionError)
    mocker.stopall()
    with lock.acquire(timeout=0):
        assert lock.is_locked
    assert lock.claims == ()


@WINDOWS_ONLY  # pragma: win32 cover
def test_strict_soft_windows_interrupted_publication_removes_claim(
    initialized_lock_path: Path, mocker: MockerFixture
) -> None:
    assert sys.platform == "win32"
    from filelock._soft._strict._windows import publish_deletable_claim

    publish = publish_deletable_claim

    def interrupt(
        private_path: Callable[[], str],
        public_path: str,
        record: bytes,
        descriptors: dict[str, int],
        deadline: float | None,
    ) -> NoReturn:
        publish(private_path, public_path, record, descriptors, deadline)
        raise KeyboardInterrupt

    lock = StrictSoftFileLock(initialized_lock_path, timeout=0)
    interruption = mocker.patch(
        "filelock._soft._strict._lock.publish_deletable_claim", side_effect=interrupt, autospec=True
    )
    with pytest.raises(KeyboardInterrupt):
        lock.acquire()
    mocker.stop(interruption)
    assert lock.claims == ()
    with lock:
        assert lock.is_locked


@WINDOWS_ONLY  # pragma: win32 cover
def test_strict_soft_windows_reclaimed_private_claim_closes_handle(tmp_path: Path, mocker: MockerFixture) -> None:
    assert sys.platform == "win32"
    from filelock._soft._strict import _windows as strict_windows

    private = tmp_path / "private.claim"
    public = tmp_path / "public.claim"

    def reclaim_then_fail(_fd: int, _path: str) -> None:
        private.unlink()
        raise OSError(EIO, "claim publication failed")

    rename = mocker.patch(
        "filelock._soft._strict._windows.rename_open_file", side_effect=reclaim_then_fail, autospec=True
    )
    with pytest.raises(OSError, match="claim publication failed"):
        strict_windows.publish_deletable_claim(lambda: str(private), str(public), b"first", {})
    mocker.stop(rename)

    os.close(strict_windows.publish_deletable_claim(lambda: str(private), str(public), b"second", {}))
    assert public.read_bytes() == b"second"


@WINDOWS_ONLY  # pragma: win32 cover
def test_strict_soft_windows_collision_reports_pin_close_error(
    initialized_lock_path: Path, mocker: MockerFixture
) -> None:
    assert sys.platform == "win32"
    lock = StrictSoftFileLock(initialized_lock_path, timeout=0)

    def fail_close() -> None:
        raise OSError(EIO, "pin close failed")

    def collide(
        _private: Callable[[], str], _public: str, _record: bytes, _descriptors: dict[str, int], _deadline: float | None
    ) -> NoReturn:
        protection = lock._context.claim_protection
        assert protection is not None
        protection.callback(fail_close)  # inject cleanup fault
        raise FileExistsError(EEXIST, "peer claim exists")

    mocker.patch("filelock._soft._strict._lock.publish_deletable_claim", side_effect=collide, autospec=True)
    with pytest.raises(OSError, match="pin close failed"):
        lock.acquire()
    assert lock.is_locked is False
