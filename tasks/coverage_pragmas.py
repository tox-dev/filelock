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


# coverage passes the config's plugin options; this plugin takes none.
def coverage_init(reg: Plugins, options: dict[str, str]) -> None:  # ruff:ignore[unused-function-argument]
    reg.add_configurer(CapabilityPragmas())


class CapabilityPragmas(CoveragePlugin):
    """Exclude ``needs``/``lacks`` capability pragmas on the runtimes where that code cannot run."""

    def configure(self, config: TConfigurable) -> None:
        excluded = [
            rf"# pragma: {'lacks' if present else 'needs'} {name}\b" for name, present in sorted(CAPABILITIES.items())
        ]
        self._extend(config, "report:exclude_lines", [*excluded, *_ALWAYS_EXCLUDED])
        # A guarded clause header only ever takes one arc, as covdefaults already assumes for its own pragmas.
        self._extend(config, "report:partial_branches", [_CAPABILITY_PRAGMA, *_ALWAYS_EXCLUDED])
        unrunnable = [
            pattern
            for name, patterns in sorted(_CAPABILITY_MODULES.items())
            if not CAPABILITIES[name]
            for pattern in patterns
        ]
        if unrunnable:
            self._extend(config, "report:omit", unrunnable)

    @staticmethod
    def _extend(config: TConfigurable, option: str, patterns: list[str]) -> None:
        # get_option is typed for every coverage option; these two always hold regexes.
        merged: set[str] = set(cast("Iterable[str]", config.get_option(option) or ()))
        merged.update(patterns)
        config.set_option(option, sorted(merged))


def _supports_symlink() -> bool:
    # Windows grants this per privilege, not per platform, so ask the filesystem.
    with tempfile.TemporaryDirectory() as directory:
        target = Path(directory, "target")
        target.touch()
        try:
            Path(directory, "link").symlink_to(target)
        except (OSError, NotImplementedError, AttributeError):
            return False
        return True


def _supports_unlinking_an_open_file() -> bool:
    # Where this is refused, a peer can never take a live holder's marker.
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
    # PyPy advertises the option then rejects it with EINVAL, so trust a real link over os.supports_follow_symlinks.
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
    # Without POSIX permission bits a chmod does not read back.
    with tempfile.TemporaryDirectory() as directory:
        probe = Path(directory, "probe")
        probe.touch()
        probe.chmod(_OWNER_READ_WRITE)
        return probe.stat().st_mode & 0o777 == _OWNER_READ_WRITE


_OWNER_READ_WRITE: Final[int] = 0o600

#: Capability -> whether this runtime provides it. Tests gate their skipif on this same mapping.
CAPABILITIES: Final[dict[str, bool]] = {
    "fork": hasattr(os, "fork") and hasattr(os, "register_at_fork"),
    "dir-fd": os.open in os.supports_dir_fd,
    "hard-link": hasattr(os, "link"),
    "symlink": _supports_symlink(),
    "fcntl": find_spec("fcntl") is not None,
    "unlink-open-file": _supports_unlinking_an_open_file(),
    "posix-signals": hasattr(signal, "SIGKILL"),
    "file-mode": _enforces_file_mode(),
    "fd-directory": any(Path(view).is_dir() for view in ("/dev/fd", "/proc/self/fd")),
    "fifo": hasattr(os, "mkfifo"),
    "af-unix": hasattr(socket, "AF_UNIX"),
    # Distinct from "symlink": a runtime can create them yet still not refuse to follow one.
    "o-nofollow": hasattr(os, "O_NOFOLLOW"),
    "utime-nofollow": os.utime in os.supports_follow_symlinks,
    "utime-fd": os.utime in os.supports_fd,
    "sqlite3": find_spec("sqlite3") is not None,
    "link-follow-symlinks": _honors_link_follow_symlinks(),
    # Only the tox env that installs a released filelock sets this.
    "old-client": bool(os.environ.get("FILELOCK_OLD_CLIENT_PATH")),
}

#: Modules a missing capability makes unrunnable in full; marking every line would restate one module-level gate.
_CAPABILITY_MODULES: Final[dict[str, tuple[str, ...]]] = {
    "hard-link": (
        "*/tests/test_strict_soft*.py",
        "*/tests\\test_strict_soft*.py",
        "*/filelock/_strict.py",
        "*\\filelock\\_strict.py",
    ),
}

#: A forked child exits through os._exit without writing coverage data, so no job in the matrix can see these.
_ALWAYS_EXCLUDED: Final[tuple[str, ...]] = (r"# pragma: forked child\b",)

_CAPABILITY_PRAGMA: Final[str] = rf"# pragma: (needs|lacks) ({'|'.join(sorted(CAPABILITIES))})\b"

__all__ = [
    "CAPABILITIES",
    "coverage_init",
]
