from __future__ import annotations

import os
import sys
from contextlib import ExitStack
from typing import TYPE_CHECKING

import pytest

if sys.platform == "win32":  # pragma: win32 cover
    from filelock._filesystem import pinned

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path


@pytest.mark.skipif(sys.platform != "win32", reason="Windows directory handles enforce rename sharing")
def test_windows_private_link_pin_blocks_rename(tmp_path: Path) -> None:  # pragma: win32 cover
    directory = tmp_path / "link"
    directory.mkdir()

    with pinned(os.fspath(directory)), pytest.raises(PermissionError) as error:
        directory.rename(tmp_path / "moved")

    assert getattr(error.value, "winerror", None) == 32


@pytest.mark.skipif(sys.platform != "win32", reason="Windows directory handles can reject junctions")
def test_windows_private_link_pin_rejects_junction(  # pragma: win32 cover
    tmp_path: Path, create_junction: Callable[[Path, Path], None]
) -> None:
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "link"
    create_junction(link, target)

    with pytest.raises(NotADirectoryError), ExitStack() as stack:
        stack.enter_context(pinned(os.fspath(link)))
