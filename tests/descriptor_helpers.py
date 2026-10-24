from __future__ import annotations

import os
from contextlib import suppress
from pathlib import Path


def descriptor_ids() -> tuple[int, ...] | range:  # pragma: needs fd-directory
    """Find descriptor candidates even when /dev/fd only lists the standard streams."""
    with Path(os.devnull).open("rb") as probe:
        for view in (Path("/dev/fd"), Path("/proc/self/fd")):
            with suppress(OSError):
                descriptors = tuple(int(entry.name) for entry in view.iterdir() if entry.name.isdecimal())
                if probe.fileno() >= 3 and probe.fileno() in descriptors:
                    return descriptors
    return range(os.sysconf("SC_OPEN_MAX"))
