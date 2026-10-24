from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING, Final

import pytest
from capabilities import CAPABILITIES

from tests.conftest import _held_open
from tests.test_read_write_fork import _descriptors_on as read_write_descriptors
from tests.test_strict_soft_failures import _descriptors_on as strict_soft_descriptors

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

pytestmark = pytest.mark.skipif(not CAPABILITIES["fd-directory"], reason="requires a descriptor directory")


@pytest.fixture(params=["complete", "partial", "missing", "proc-only"])
def descriptor_view(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:  # pragma: needs fd-directory
    iterdir: Final = Path.iterdir
    native: Final = Path("/dev/fd") if Path("/dev/fd").is_dir() else Path("/proc/self/fd")
    limit: Final = max(int(entry.name) for entry in iterdir(native)) + 64

    def entries(path: Path) -> Iterator[Path]:
        if path == Path("/dev/fd"):
            if request.param == "complete":
                return iterdir(native)
            if request.param in {"partial", "proc-only"}:
                return iter(path / str(number) for number in range(3))
            raise FileNotFoundError(path)
        if path == Path("/proc/self/fd"):
            if request.param == "proc-only":
                return iterdir(native)
            raise FileNotFoundError(path)
        return iterdir(path)

    monkeypatch.setattr(Path, "iterdir", entries)
    monkeypatch.setattr(os, "sysconf", lambda _name: limit)


@pytest.mark.usefixtures("descriptor_view")
@pytest.mark.parametrize("count", [read_write_descriptors, strict_soft_descriptors])
def test_descriptor_counts_follow_real_open_files(  # pragma: needs fd-directory
    tmp_path: Path, count: Callable[[Path], int]
) -> None:
    path: Final = tmp_path / "held"
    with path.open("w+b") as first:
        second = os.dup(first.fileno())
        try:
            assert count(path) == 2
        finally:
            os.close(second)
        assert count(path) == 1
    assert count(path) == 0


@pytest.mark.usefixtures("descriptor_view")
def test_inode_reuse_waits_for_an_unlinked_files_last_descriptor(tmp_path: Path) -> None:  # pragma: needs fd-directory
    path: Final = tmp_path / "unlinked"
    with path.open("w+b") as held:
        node = os.fstat(held.fileno())
        path.unlink()
        assert _held_open(node)
    assert not _held_open(node)


@pytest.mark.usefixtures("descriptor_view")
def test_descriptor_view_preserves_unrelated_directory_entries(tmp_path: Path) -> None:  # pragma: needs fd-directory
    child = tmp_path / "ordinary-file"
    child.touch()
    assert list(tmp_path.iterdir()) == [child]
