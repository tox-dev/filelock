from __future__ import annotations

import os
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from pytest_mock import MockerFixture


def reuse_inode(mocker: MockerFixture, successor: Path, freed: os.stat_result) -> None:  # pragma: win32 no cover
    """
    Make ``os.lstat`` report *successor* under the inode number of the unlinked *freed* once nothing holds that open.

    ext4 hands a freed inode number to the next file created, which a tmpfs test directory never does, so this stands in
    for the filesystem: the number moves only while no descriptor in this process keeps the old inode allocated.
    """
    created: Final = successor.lstat()
    lstat: Final = os.lstat

    def lstat_with_reuse(path: str | os.PathLike[str], *, dir_fd: int | None = None) -> os.stat_result:
        found: Final = lstat(path, dir_fd=dir_fd)
        if (found.st_dev, found.st_ino) != (created.st_dev, created.st_ino) or _held_open(freed):
            return found
        # The tuple carries the integer fields up to st_size; the float and nanosecond times ride in the mapping.
        extra: Final = {
            name: getattr(found, name)
            for name in dir(found)
            if name.startswith("st_")
            and name not in {"st_mode", "st_ino", "st_dev", "st_nlink", "st_uid", "st_gid", "st_size"}
        }
        return os.stat_result((found.st_mode, freed.st_ino, *found[2:10]), extra)

    mocker.patch("os.lstat", side_effect=lstat_with_reuse)


def _held_open(node: os.stat_result) -> bool:  # pragma: win32 no cover
    for entry in Path("/dev/fd").iterdir():
        with suppress(OSError):  # the descriptor the listing itself used is closed by now
            if os.path.samestat(os.fstat(int(entry.name)), node):
                return True
    return False


__all__ = [
    "reuse_inode",
]
