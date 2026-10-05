from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Final, Literal

import pytest
from capabilities import CAPABILITIES

pytest.importorskip("sqlite3")

from filelock import AsyncReadWriteLock, ReadWriteLock
from tests.read_write_helpers import assert_read_write_lock_state

if TYPE_CHECKING:
    from pytest_mock import MockerFixture

pytestmark: Final = [
    pytest.mark.requires_hard_links,
    pytest.mark.skipif(
        not CAPABILITIES["posix-hard-link"],
        reason="private database aliases need POSIX hard links without following symlinks",
    ),
]


@pytest.mark.parametrize("mode", [pytest.param("read", id="read"), pytest.param("write", id="write")])
def test_missing_descriptor_path_preserves_contention(database: Path, mode: Literal["read", "write"]) -> None:
    lock: Final = ReadWriteLock(database, is_singleton=False)
    with (lock.read_lock if mode == "read" else lock.write_lock)():
        assert_read_write_lock_state(str(database), "write", available=False)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", [pytest.param("read", id="read"), pytest.param("write", id="write")])
async def test_missing_descriptor_path_preserves_async_contention(
    database: Path, mode: Literal["read", "write"]
) -> None:
    lock: Final = AsyncReadWriteLock(database, is_singleton=False)
    async with (lock.read_lock if mode == "read" else lock.write_lock)():
        assert_read_write_lock_state(str(database), "write", available=False)
    await lock.close()


def test_missing_descriptor_path_removes_alias_after_release(database: Path, created_directories: list[Path]) -> None:
    lock: Final = ReadWriteLock(database, is_singleton=False)
    lock.acquire_read()
    assert [directory.exists() for directory in created_directories] == [False, True]
    lock.release()
    assert [directory.exists() for directory in created_directories] == [False, False]


@pytest.mark.parametrize(
    "boundary",
    [pytest.param("os.link", id="link"), pytest.param("sqlite3.connect", id="connect")],
)
def test_missing_descriptor_path_cleans_up_after_failure(
    database: Path, mocker: MockerFixture, boundary: str, created_directories: list[Path]
) -> None:
    mocker.patch(boundary, autospec=True, side_effect=OSError("cannot open database"))
    with pytest.raises(OSError, match="cannot open database"):
        ReadWriteLock(database, is_singleton=False)
    assert [directory.exists() for directory in created_directories] == [False]


@pytest.mark.parametrize("replacement_kind", [pytest.param("file", id="file"), pytest.param("symlink", id="symlink")])
def test_missing_descriptor_path_rejects_replaced_database(
    database: Path, tmp_path: Path, mocker: MockerFixture, replacement_kind: str, created_directories: list[Path]
) -> None:
    replacement: Final = tmp_path / "replacement.db"
    if replacement_kind == "file":
        replacement.touch()
    else:
        replacement.symlink_to(tmp_path / "victim.db")
    link: Final = os.link

    def replace_and_link(source: str, destination: Path, *, follow_symlinks: bool) -> None:
        replacement.replace(source)
        link(source, destination, follow_symlinks=follow_symlinks)

    mocker.patch("os.link", autospec=True, side_effect=replace_and_link)
    with pytest.raises(OSError, match="database changed"):
        ReadWriteLock(database, is_singleton=False)
    assert ([directory.exists() for directory in created_directories], (tmp_path / "victim.db").exists()) == (
        [False],
        False,
    )


@pytest.fixture
def database(tmp_path: Path) -> Path:
    return tmp_path / "lock.db"


@pytest.fixture
def created_directories(mocker: MockerFixture) -> list[Path]:
    directories: Final[list[Path]] = []
    mkdtemp: Final = tempfile.mkdtemp

    def create_directory(*, prefix: str) -> str:
        directory: Final = mkdtemp(prefix=prefix)
        directories.append(Path(directory))
        return directory

    mocker.patch("tempfile.mkdtemp", autospec=True, side_effect=create_directory)
    return directories


@pytest.fixture(autouse=True)
def missing_descriptor_path(mocker: MockerFixture) -> None:
    mocker.patch("os.access", autospec=True, return_value=False)
