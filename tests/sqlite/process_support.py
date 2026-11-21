from __future__ import annotations

import contextlib
import os
import signal
import subprocess  # ruff:ignore[suspicious-subprocess-import]  # launches the child reader and Windows ACL tool
import sys
from contextlib import contextmanager
from multiprocessing import set_start_method
from pathlib import Path
from typing import TYPE_CHECKING, Final, Literal, NamedTuple

import pytest
from capabilities import CAPABILITIES

from filelock import ReadWriteLock

if TYPE_CHECKING:
    from collections.abc import Generator
    from multiprocessing.synchronize import Event as EventType


PROCESS_DEADLINE: Final[int] = 30
NEEDS_FD_DIRECTORY: Final[pytest.MarkDecorator] = pytest.mark.skipif(
    not CAPABILITIES["fd-directory"], reason="counting open descriptors needs /dev/fd or /proc/self/fd"
)


if sys.implementation.name == "pypy":
    set_start_method(
        "spawn", force=True
    )  # pragma: no cover  # exercised only under the pypy fork backend, which runs without coverage


def acquire_lock(
    lock_file: str,
    mode: Literal["read", "write"],
    acquired_event: EventType,
    release_event: EventType | None = None,
    timeout: float = -1,
    blocking: bool = True,
    ready_event: EventType | None = None,
    contending_event: EventType | None = None,
) -> None:
    if ready_event:
        ready_event.wait(timeout=PROCESS_DEADLINE)

    lock = ReadWriteLock(lock_file, timeout=timeout, blocking=blocking)
    if contending_event:
        contending_event.set()  # interpreter start-up is over, so a caller can time the contention itself
    with getattr(lock, f"{mode}_lock")():
        acquired_event.set()
        if release_event:
            release_event.wait(timeout=PROCESS_DEADLINE)


@contextmanager
def holding_reader(database: Path) -> Generator[subprocess.Popen[str]]:  # pragma: needs sqlite-private-link
    script: Final[str] = (
        "import sys, time\n"
        "from filelock import ReadWriteLock\n"
        "lock = ReadWriteLock(sys.argv[1], is_singleton=False)\n"
        "lock.acquire_read()\n"
        "print('held', flush=True)\n"
        "time.sleep(600)\n"
    )
    with subprocess.Popen([sys.executable, "-c", script, str(database)], stdout=subprocess.PIPE, text=True) as holder:
        try:
            assert holder.stdout is not None
            assert holder.stdout.readline() == "held\n"
            yield holder
        finally:
            holder.kill()
            holder.wait(timeout=30)


def grant_everyone_write(directory: Path) -> None:  # pragma: win32 cover
    icacls = Path(os.environ["SYSTEMROOT"], "System32", "icacls.exe")
    subprocess.run([icacls, directory, "/grant", "*S-1-1-0:(OI)(CI)(M)"], capture_output=True, check=True)


def run_fork_script(script: str, arguments: list[str], *, timeout: float) -> _ForkResult:
    process = subprocess.Popen(  # executes this test's fixed interpreter and source
        [sys.executable, "-c", script, *arguments],
        start_new_session=True,
        stderr=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        process_output, process_error = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as error:
        with contextlib.suppress(ProcessLookupError):
            if sys.platform == "win32":  # pragma: win32 cover
                process.kill()
            else:  # pragma: win32 no cover
                os.killpg(process.pid, signal.SIGKILL)
        process_output, process_error = process.communicate()
        msg = f"fork script exceeded {timeout} seconds: stdout={process_output!r}, stderr={process_error!r}"
        raise AssertionError(msg) from error
    assert process.returncode is not None  # pragma: needs fork
    return _ForkResult(process.returncode, process_output, process_error)  # pragma: needs fork


class _ForkResult(NamedTuple):
    returncode: int
    output: str
    error: str


__all__ = [
    "NEEDS_FD_DIRECTORY",
    "PROCESS_DEADLINE",
    "acquire_lock",
    "grant_everyone_write",
    "holding_reader",
    "run_fork_script",
]
