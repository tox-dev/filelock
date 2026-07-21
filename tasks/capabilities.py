"""What the runtime this suite is running on can actually do.

Probed rather than inferred from a platform or interpreter name: a name answers who is running, and every question
here is about what the runtime can do. The coverage pragmas and the tests' skipif gates both read these, so a test
cannot skip while coverage still demands its lines.

Kept apart from the pragma plugin so reading a capability never requires coverage to be installed.
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
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Generator, Iterator


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


class _Suspend:
    """An awaitable that parks its coroutine once, so a probe can throw into a suspended frame without a loop."""

    def __await__(self) -> Generator[None, None, None]:
        yield


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


_AUDIT_PROBE_EVENT: Final[str] = "filelock.capability-probe"


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
    # Narrower than "fork": GraalPy registers fork handlers but cannot fork.
    "register-at-fork": hasattr(os, "register_at_fork"),
    "dir-fd": os.open in os.supports_dir_fd,
    # Narrower than "dir-fd": GraalPy takes os.open relative to a directory descriptor but not os.link.
    "link-dir-fd": hasattr(os, "link") and os.link in os.supports_dir_fd,
    "fork1": hasattr(os, "fork1"),
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
    # A source consumer may run the suite unmeasured, and a forked child then has nothing to flush.
    "coverage": find_spec("coverage") is not None,
    "link-follow-symlinks": _honors_link_follow_symlinks(),
    # Only the tox env that installs a released filelock sets this.
    "old-client": bool(os.environ.get("FILELOCK_OLD_CLIENT_PATH")),
}


__all__ = [
    "CAPABILITIES",
]
