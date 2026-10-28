"""Counts the descriptors this process holds on an inode."""

from __future__ import annotations

import os
import sys
from contextlib import suppress
from pathlib import Path

from capabilities import CAPABILITIES


def descriptors_on(node: os.stat_result) -> int:  # pragma: needs fd-directory
    if CAPABILITIES["complete-fd-directory"]:
        view = Path("/dev/fd") if Path("/dev/fd").is_dir() else Path("/proc/self/fd")
        descriptor_ids = [int(entry.name) for entry in view.iterdir()]
    else:
        # FreeBSD's devfs and NetBSD list only the standard streams under /dev/fd until fdescfs is mounted, so every
        # number the process may hold is probed instead.
        assert sys.platform != "win32"  # narrows os.sysconf for ty
        descriptor_ids = range(os.sysconf("SC_OPEN_MAX"))
    count = 0
    for descriptor_id in descriptor_ids:
        with suppress(OSError):  # the descriptor that listed the directory is already closed
            count += os.path.samestat(os.fstat(descriptor_id), node)
    return count


__all__ = [
    "descriptors_on",
]
