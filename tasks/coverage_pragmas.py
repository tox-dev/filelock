"""Coverage exclusions keyed on the capability that code needs.

covdefaults keys its pragmas on ``os.name``, ``sys.platform`` and ``sys.implementation.name``, so code that cannot run
for a capability reason has to name a platform as a stand-in. The stand-in goes stale. A ``win32 no cover`` sat on
``os.link``'s follow_symlinks fallback and demanded a branch modern Windows never takes, and the same guess hid real
Windows gaps behind platform-agnostic code.

``# pragma: needs <capability>`` drops out only where the capability is absent, and ``# pragma: lacks <capability>``
only where it is present. Both read the probes below, as do the tests' skipif gates, so a test cannot skip while
coverage still demands its lines.

List this after covdefaults in ``[tool.coverage] run.plugins``; both merge into the same options.
"""

from __future__ import annotations

import os
import signal
import socket
import tempfile
from importlib.util import find_spec
from pathlib import Path
from typing import TYPE_CHECKING, Final, cast

from coverage import CoveragePlugin

if TYPE_CHECKING:
    from collections.abc import Iterable

    from coverage.plugin_support import Plugins
    from coverage.types import TConfigurable


# coverage's plugin entry point; it passes the config's plugin options, which this plugin does not take.
def coverage_init(reg: Plugins, options: dict[str, str]) -> None:  # ruff:ignore[unused-function-argument]
    reg.add_configurer(CapabilityPragmas())


class CapabilityPragmas(CoveragePlugin):
    """Exclude ``needs``/``lacks`` capability pragmas on the runtimes where that code cannot run."""

    def configure(self, config: TConfigurable) -> None:
        # `needs` guards code a capability makes possible; `lacks` guards the fallback a runtime without it takes, such
        # as reacquiring a lock whose name Windows would not let the holder unlink. Each is dead where the other runs.
        excluded = [
            rf"# pragma: {'lacks' if present else 'needs'} {name}\b" for name, present in sorted(CAPABILITIES.items())
        ]
        self._extend(config, "report:exclude_lines", [*excluded, *_ALWAYS_EXCLUDED])
        # A guarded clause header only ever takes one arc, so record every capability pragma as a partial branch the
        # way covdefaults does for its platform pragmas, whether or not this runtime excludes it.
        self._extend(config, "report:partial_branches", [_CAPABILITY_PRAGMA, *_ALWAYS_EXCLUDED])

    @staticmethod
    def _extend(config: TConfigurable, option: str, patterns: list[str]) -> None:
        # get_option is typed for every option coverage has; the two this plugin extends always hold a list of regexes.
        merged: set[str] = set(cast("Iterable[str]", config.get_option(option) or ()))
        merged.update(patterns)
        config.set_option(option, sorted(merged))


def _supports_symlink() -> bool:
    # Windows does support symlinks, but only with Developer Mode or SeCreateSymbolicLinkPrivilege, so ask the
    # filesystem instead of the platform name. CI runs a Windows job with the privilege and one without.
    with tempfile.TemporaryDirectory() as directory:
        target = Path(directory, "target")
        target.touch()
        try:
            Path(directory, "link").symlink_to(target)
        except (OSError, NotImplementedError, AttributeError):
            return False
        return True


def _supports_unlinking_an_open_file() -> bool:
    # POSIX unlinks a name out from under an open descriptor; Windows refuses while any handle is held, which is why a
    # peer there can never take a live holder's marker.
    with tempfile.TemporaryDirectory() as directory:
        victim = Path(directory, "victim")
        victim.touch()
        with victim.open("rb"):
            try:
                victim.unlink()
            except OSError:
                return False
        return True


def _honors_link_follow_symlinks() -> bool:
    # PyPy advertises follow_symlinks for os.link then rejects it with EINVAL, so link a real file rather than trust
    # os.supports_follow_symlinks; filelock._strict probes the same way.
    if not hasattr(os, "link"):
        return False
    with tempfile.TemporaryDirectory() as directory:
        source = Path(directory, "source")
        source.touch()
        try:
            os.link(source, Path(directory, "link"), follow_symlinks=False)
        except (OSError, NotImplementedError, ValueError):
            return False
        return True


def _enforces_file_mode() -> bool:
    # Windows does not carry POSIX permission bits, so a chmod there does not read back.
    with tempfile.TemporaryDirectory() as directory:
        probe = Path(directory, "probe")
        probe.touch()
        probe.chmod(_OWNER_READ_WRITE)
        return probe.stat().st_mode & 0o777 == _OWNER_READ_WRITE


_OWNER_READ_WRITE: Final[int] = 0o600

#: Capability -> whether this runtime provides it. The name states why the code it guards cannot run here, which a
#: bare platform pragma never said. Tests gate their skipif on this same mapping.
CAPABILITIES: Final[dict[str, bool]] = {
    # Windows has neither os.fork nor os.register_at_fork, so fork coordination never runs there.
    "fork": hasattr(os, "fork") and hasattr(os, "register_at_fork"),
    # Directory-descriptor syscalls (openat and friends) exist only where os.supports_dir_fd lists os.open.
    "dir-fd": os.open in os.supports_dir_fd,
    # Termux/Android ships a CPython whose os module has no link(), so hard-linked claims cannot be published.
    "hard-link": hasattr(os, "link"),
    "symlink": _supports_symlink(),
    # fcntl backs the flock path; absent on Windows and on builds that drop the module.
    "fcntl": find_spec("fcntl") is not None,
    "unlink-open-file": _supports_unlinking_an_open_file(),
    # SIGKILL and friends; a test that kills a worker outright cannot run without them.
    "posix-signals": hasattr(signal, "SIGKILL"),
    "file-mode": _enforces_file_mode(),
    # Counting a process's own open descriptors needs /dev/fd or /proc/self/fd; Windows exposes neither.
    "fd-directory": any(Path(view).is_dir() for view in ("/dev/fd", "/proc/self/fd")),
    # Staging a lock file as a node that is not a regular file.
    "fifo": hasattr(os, "mkfifo"),
    "af-unix": hasattr(socket, "AF_UNIX"),
    # Refusing to open through a symlink. Distinct from "symlink": a Windows runtime with the privilege can create
    # symlinks yet still cannot refuse to follow one.
    "o-nofollow": hasattr(os, "O_NOFOLLOW"),
    # Stamping a symlink's own mtime rather than its target's.
    "utime-nofollow": os.utime in os.supports_follow_symlinks,
    # Refreshing a marker through the verified descriptor instead of re-resolving its path.
    "utime-fd": os.utime in os.supports_fd,
    # The read-write lock is backed by sqlite3, which a stripped build can omit; CI runs a job without it.
    "sqlite3": find_spec("sqlite3") is not None,
    "link-follow-symlinks": _honors_link_follow_symlinks(),
}

#: A forked child exits through os._exit without writing its coverage data, so lines that run only in the child stay
#: invisible to every job in the matrix.
_ALWAYS_EXCLUDED: Final[tuple[str, ...]] = (r"# pragma: forked child\b",)

_CAPABILITY_PRAGMA: Final[str] = rf"# pragma: (needs|lacks) ({'|'.join(sorted(CAPABILITIES))})\b"

__all__ = [
    "CAPABILITIES",
    "coverage_init",
]
