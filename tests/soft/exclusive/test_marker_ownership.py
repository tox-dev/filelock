from __future__ import annotations

import os
from errno import ENOSYS
from pathlib import Path
from stat import filemode
from typing import TYPE_CHECKING

import pytest

import filelock._soft._exclusive._publication  # simplint: ignore[private-import-path]  # tests internal behavior
from filelock import (
    SoftFileLock,
)
from tests.capability_marks import (
    NEEDS_FILE_MODE,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from pytest_mock import MockerFixture


def test_cleanup_soft_lock(tmp_path: Path) -> None:
    lock_path = tmp_path / "a"

    with SoftFileLock(lock_path):
        assert lock_path.exists()
    assert not lock_path.exists()


@NEEDS_FILE_MODE
@pytest.mark.parametrize(
    ("make_lock", "umask"),
    [
        pytest.param(lambda path: SoftFileLock(path, mode=0o666), 0o022, id="explicit-mode"),
        pytest.param(SoftFileLock, 0, id="default-mode-open-umask"),
    ],
)
def test_lock_mode_soft_lets_only_the_owner_write_the_marker(  # pragma: needs file-mode
    make_lock: Callable[[str], SoftFileLock], umask: int, tmp_path: Path
) -> None:
    # Contenders act on the record inside, so no other account may rewrite it.
    lock_path = tmp_path / "a.lock"
    lock = make_lock(str(lock_path))

    initial_umask = os.umask(umask)
    try:
        with lock:
            assert filemode(lock_path.stat().st_mode) == "-rw-r--r--"
    finally:
        os.umask(initial_umask)


def test_umask_soft(tmp_path: Path) -> None:
    lock = SoftFileLock(str(tmp_path / "a.lock"), mode=0o666)

    initial_umask = os.umask(0)
    os.umask(initial_umask)

    lock.acquire()
    assert lock.is_locked

    current_umask = os.umask(0)
    os.umask(current_umask)
    assert initial_umask == current_umask

    lock.release()


def test_soft_errors(tmp_path: Path, mocker: MockerFixture, soft_create_path: str) -> None:
    mocker.patch(soft_create_path, side_effect=OSError(ENOSYS, "mock error"), autospec=True)
    with pytest.raises(OSError, match="mock error"):
        SoftFileLock(tmp_path / "a.lock").acquire()


def test_soft_windows_unlink_stops_on_non_permission_error(tmp_path: Path, mocker: MockerFixture) -> None:
    path = tmp_path / "windows.lock"
    path.write_bytes(b"x")
    lock = SoftFileLock(str(path))
    st = os.lstat(path)
    mocker.patch(
        "filelock._soft._exclusive._publication.unlink_path",
        new=mocker.create_autospec(
            filelock._soft._exclusive._publication.unlink_path, side_effect=OSError(ENOSYS, "boom")
        ),
    )

    lock._windows_unlink_if_ours((st.st_dev, st.st_ino))

    assert path.exists()


def test_soft_marker_omits_start_line_without_a_start_token(tmp_path: Path, mocker: MockerFixture) -> None:
    mocker.patch(
        "filelock._soft._exclusive._publication.process_start_token",
        new=mocker.create_autospec(filelock._soft._exclusive._publication.process_start_token, return_value=None),
    )
    lock = SoftFileLock(str(tmp_path / "a"))
    with lock:
        lines = Path(lock.lock_file).read_text(encoding="utf-8").splitlines()

    assert len(lines) == 2
    assert lines[0] == str(os.getpid())


def test_soft_is_lock_held_by_us_false_for_unparseable_marker(tmp_path: Path) -> None:
    lock_path = tmp_path / "a"
    lock_path.write_text("garbage-not-a-marker", encoding="utf-8")
    lock = SoftFileLock(str(lock_path))

    assert lock.is_lock_held_by_us is False
