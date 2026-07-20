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

import gc
import os
import signal
import socket
import sys
import tempfile
import weakref
from asyncio import CancelledError
from contextlib import asynccontextmanager, contextmanager
from importlib.util import find_spec
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, cast

from coverage import CoveragePlugin

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Generator, Iterable, Iterator

    from coverage.plugin_support import Plugins
    from coverage.types import TConfigurable


class _Suspend:
    """An awaitable that parks its coroutine once, so a probe can throw into a suspended frame without a loop."""

    def __await__(self) -> Generator[None, Any, None]:
        yield


_AUDIT_PROBE_EVENT: Final[str] = "filelock.capability-probe"


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


def _finalizes_on_last_reference() -> bool:
    # Only a refcounting collector runs __del__ the moment the last reference goes; a tracing one defers it.
    finalized: list[bool] = []

    class Probe:
        def __del__(self) -> None:
            finalized.append(True)

    Probe()
    return bool(finalized)


def _finalizes_on_collection() -> bool:
    # GraalPy queues finalizers on the host collector, so even a forced collection does not run __del__.
    finalized: list[bool] = []

    class Probe:
        def __del__(self) -> None:
            finalized.append(True)

    Probe()
    gc.collect()
    return bool(finalized)


def _collects_classes() -> bool:
    # A dynamically built lock subclass must not outlive its last reference, or the registries keyed on it leak.
    def build() -> type:
        class Probe:
            pass

        return Probe

    reference = weakref.ref(build())
    gc.collect()
    return reference() is None


def _preserves_context_thrown_into_a_generator() -> bool:
    # GraalPy resets __context__ when contextlib throws into the suspended generator, losing the chained cause.
    @contextmanager
    def probe() -> Iterator[None]:
        yield

    error = KeyError("thrown")
    error.__context__ = ValueError("context")
    try:
        with probe():
            raise error
    except KeyError as caught:
        return caught.__context__ is not None
    return False  # pragma: no cover  # the raise above always propagates


def _propagates_a_cancellation_thrown_into_a_coroutine() -> bool:
    # Driven by hand so the probe needs no event loop: GraalPy answers athrow with RuntimeError instead of the
    # CancelledError, so every cancellation crossing an async context manager surfaces as the wrong exception.
    @asynccontextmanager
    async def gate() -> AsyncIterator[None]:
        await _Suspend()
        yield

    async def body() -> None:
        async with gate():
            pass

    coroutine = body()
    coroutine.send(None)
    try:
        coroutine.throw(CancelledError())
    except CancelledError:
        return True
    except BaseException:  # ruff:ignore[blind-except]  # whatever else a runtime substitutes counts as the deviation
        return False
    return False  # pragma: no cover  # throwing into the suspended coroutine always raises


def _delivers_audit_events() -> bool:
    # GraalPy accepts a hook and never calls it. The hook is a permanent no-op; tests install their own anyway.
    delivered: list[bool] = []
    sys.addaudithook(lambda event, _args: delivered.append(True) if event == _AUDIT_PROBE_EVENT else None)
    sys.audit(_AUDIT_PROBE_EVENT)
    return bool(delivered)


def _refuses_to_open_a_symlink() -> bool:
    # GraalPy accepts O_NOFOLLOW then follows the link anyway, so ask for the refusal rather than the constant.
    if not hasattr(os, "O_NOFOLLOW"):
        return False
    if not _supports_symlink():
        # Nothing to point the probe at, so keep the constant's answer rather than reporting a gap we cannot see.
        return True
    with tempfile.TemporaryDirectory() as directory:
        target = Path(directory, "target")
        target.touch()
        link = Path(directory, "link")
        link.symlink_to(target)
        try:
            descriptor = os.open(link, os.O_RDONLY | os.O_NOFOLLOW)
        except OSError:
            return True
        os.close(descriptor)
        return False


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
    # Narrower than "dir-fd": GraalPy takes os.open relative to a directory descriptor but not os.link.
    "link-dir-fd": hasattr(os, "link") and os.link in os.supports_dir_fd,
    "hard-link": hasattr(os, "link"),
    "symlink": _supports_symlink(),
    "fcntl": find_spec("fcntl") is not None,
    "unlink-open-file": _supports_unlinking_an_open_file(),
    "posix-signals": hasattr(signal, "SIGKILL"),
    "file-mode": _enforces_file_mode(),
    "prompt-finalization": _finalizes_on_last_reference(),
    "collected-finalization": _finalizes_on_collection(),
    "class-collection": _collects_classes(),
    "generator-exception-context": _preserves_context_thrown_into_a_generator(),
    "coroutine-cancellation": _propagates_a_cancellation_thrown_into_a_coroutine(),
    "audit-events": _delivers_audit_events(),
    "fd-directory": any(Path(view).is_dir() for view in ("/dev/fd", "/proc/self/fd")),
    "fifo": hasattr(os, "mkfifo"),
    "af-unix": hasattr(socket, "AF_UNIX"),
    # Distinct from "symlink": a runtime can create them yet still not refuse to follow one.
    "o-nofollow": _refuses_to_open_a_symlink(),
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
