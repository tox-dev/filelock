"""Coverage exclusions keyed on the capability that code needs.

covdefaults keys its pragmas on ``os.name``, ``sys.platform`` and ``sys.implementation.name``, so code that cannot run
for a capability reason has to name a platform as a stand-in. The stand-in goes stale. A ``win32 no cover`` sat on
``os.link``'s follow_symlinks fallback and demanded a branch modern Windows never takes, and the same guess hid real
Windows gaps behind platform-agnostic code.

``# pragma: needs <capability>`` drops out only where the capability is absent, and ``# pragma: lacks <capability>``
only where it is present. Both read the probes in :mod:`capabilities`, as do the tests' skipif gates, so a test cannot
skip while coverage still demands its lines.

List this after covdefaults in ``[tool.coverage] run.plugins``; both merge into the same options.
"""

from __future__ import annotations

from types import MappingProxyType
from typing import TYPE_CHECKING, Final, cast

from capabilities import CAPABILITIES
from coverage import CoveragePlugin

if TYPE_CHECKING:
    from collections.abc import Iterable

    from coverage.plugin_support import Plugins
    from coverage.types import TConfigurable


# coverage passes the config's plugin options; this plugin takes none.
def coverage_init(reg: Plugins, options: dict[str, str]) -> None:  # ruff:ignore[unused-function-argument]
    reg.add_configurer(_CapabilityPragmas())


class _CapabilityPragmas(CoveragePlugin):
    # Exclude ``needs``/``lacks`` capability pragmas on the runtimes where that code cannot run.
    def configure(self, config: TConfigurable) -> None:
        excluded = [
            rf"# pragma: {'lacks' if present else 'needs'} {name}\b" for name, present in sorted(CAPABILITIES.items())
        ]
        # A forked child exits through os._exit without writing coverage data, so no job in the matrix can see these.
        always_excluded: tuple[str, ...] = (r"# pragma: forked child\b",)
        self._extend(config, "report:exclude_lines", [*excluded, *always_excluded])
        # A guarded clause header only ever takes one arc, as covdefaults already assumes for its own pragmas.
        self._extend(config, "report:partial_branches", [_CAPABILITY_PRAGMA, *always_excluded])
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


#: Modules a missing capability makes unrunnable in full; marking every line would restate one module-level gate.
_CAPABILITY_MODULES: Final[MappingProxyType[str, tuple[str, ...]]] = MappingProxyType({
    "posix-hard-link": ("*/tests/sqlite/test_links*.py", "*/tests/sqlite/link_support.py"),
    "hard-link": (
        "*/tests/soft/read_write/*.py",
        "*/tests/soft/strict/claim_records.py",
        "*/tests/soft/strict/filesystem_faults.py",
        "*/tests/soft/strict/test_claims.py",
        "*/tests/soft/strict/test_compatibility.py",
        "*/tests/soft/strict/test_directories*.py",
        "*/tests/soft/strict/test_lock*.py",
        "*/tests/soft/strict/test_publication*.py",
        "*/tests/soft/strict/test_recovery.py",
        "*/tests/soft/strict/test_scan*.py",
        "*/tests/soft/strict/test_sentinel.py",
        "*/tests/soft/strict/test_windows*.py",
        "*/filelock/_soft/_read_write/*.py",
        "*/filelock/_soft/_strict/_claims.py",
        "*/filelock/_soft/_strict/_directories.py",
        "*/filelock/_soft/_strict/_lock.py",
        "*/filelock/_soft/_strict/_publication.py",
        "*/filelock/_soft/_strict/_release.py",
        "*/filelock/_soft/_strict/_scan.py",
        "*/filelock/_soft/_strict/_sentinel.py",
    ),
})

_CAPABILITY_PRAGMA: Final[str] = rf"# pragma: (needs|lacks) ({'|'.join(sorted(CAPABILITIES))})\b"

__all__ = [
    "coverage_init",
]
