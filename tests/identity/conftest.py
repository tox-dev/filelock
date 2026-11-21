from __future__ import annotations

import importlib
import io
import os
import sys
from errno import ENOENT
from pathlib import Path
from typing import TYPE_CHECKING, Final

import pytest

from filelock._identity import process_alive

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from pytest_mock import MockerFixture


@pytest.fixture
def hide_boot_id(mocker: MockerFixture) -> Iterator[Callable[[], None]]:  # pragma: linux cover
    # systemd's ProcSubset=pid can hide the boot ID at import.
    def hide() -> None:
        # The boot id is the only text file the module reads at import.
        hidden = mocker.patch.object(
            Path, "read_text", autospec=True, side_effect=FileNotFoundError(ENOENT, "No such file or directory")
        )
        importlib.reload(sys.modules[process_alive.__module__])
        mocker.stop(hidden)

    yield hide
    importlib.reload(sys.modules[process_alive.__module__])


@pytest.fixture
def mount_over_boot_id(mocker: MockerFixture) -> Iterator[Callable[[bool], None]]:  # pragma: linux cover
    def mount_over(same_device: bool) -> None:
        path: Final[Path] = Path("/proc/sys/kernel/random/boot_id")
        real_stat = Path.stat
        proc_device: Final[int] = real_stat(Path("/proc")).st_dev
        mounted_stat = os.stat_result((0, 0, proc_device if same_device else proc_device + 1, 0, 0, 0, 0, 0, 0, 0))

        def stat(current: Path, *, follow_symlinks: bool = True) -> os.stat_result:
            return mounted_stat if current == path else real_stat(current, follow_symlinks=follow_symlinks)

        stat_patch = mocker.patch.object(Path, "stat", autospec=True, side_effect=stat)
        text_patch = mocker.patch.object(
            Path, "read_text", autospec=True, return_value="00000000-0000-4000-8000-000000000001\n"
        )
        open_patch = mocker.patch.object(
            Path,
            "open",
            autospec=True,
            return_value=io.BytesIO(f"1 0 0:{proc_device} /sys/kernel/random/uuid {path} rw - proc proc rw\n".encode()),
        )
        importlib.reload(sys.modules[process_alive.__module__])
        mocker.stop(open_patch)
        mocker.stop(text_patch)
        mocker.stop(stat_patch)

    yield mount_over
    importlib.reload(sys.modules[process_alive.__module__])
