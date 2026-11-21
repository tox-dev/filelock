from __future__ import annotations

import gc
import os
from errno import EBADF
from typing import TYPE_CHECKING, Final, Literal, NoReturn

import pytest
from capabilities import CAPABILITIES

from filelock import AcquireReturnProxy, BaseFileLock, Timeout
from tests.fork_helpers import exit_child, fork_process

if TYPE_CHECKING:
    from collections.abc import Callable

FORK_WARNING: Final[pytest.MarkDecorator] = pytest.mark.filterwarnings(
    "ignore:.*multi-threaded, use of fork.*:DeprecationWarning"
)

NEEDS_SETTRACE_SAFE_AUDIT_HOOKS: Final[pytest.MarkDecorator] = pytest.mark.skipif(
    not CAPABILITIES["settrace-safe-audit-hooks"],
    reason="Python audit hooks interrupt concurrent sys.settrace calls on this runtime",
)


def fork_descriptor_probe(  # pragma: needs fork
    descriptor: int, stat_descriptor: Callable[[int], os.stat_result]
) -> int:
    def probe_child() -> NoReturn:
        with pytest.raises(OSError, match=rf"\[Errno {EBADF}\]"):
            stat_descriptor(descriptor)
        exit_child(0)

    return fork_process(probe_child)


def error_details(group: BaseExceptionGroup) -> list[tuple[type[BaseException], str]]:
    return [(type(error), str(error)) for error in group.exceptions]


def cleanup_child_hold(
    lock: BaseFileLock,
    proxy: AcquireReturnProxy,
    action: Literal["release", "context", "collect"],
) -> NoReturn:
    assert (lock.is_locked, lock.lock_counter) == (False, 0)
    if action == "release":
        lock.release(force=True)
    elif action == "context":
        proxy.__exit__(None, None, None)
    else:
        del proxy
        del lock
        gc.collect()
    exit_child(0)


def probe_lock(lock_type: type[BaseFileLock], path: str) -> bool:  # pragma: needs fork
    read_fd, write_fd = os.pipe()

    def probe_child() -> NoReturn:
        os.close(read_fd)
        candidate = lock_type(path, is_singleton=False)
        try:
            candidate.acquire(timeout=0)
        except Timeout:
            acquired = False
        else:
            acquired = True
            candidate.release()
        os.write(write_fd, b"1" if acquired else b"0")
        os.close(write_fd)
        exit_child(0)

    child_pid = fork_process(probe_child)
    os.close(write_fd)
    result = os.read(read_fd, 1)
    os.close(read_fd)
    _, status = os.waitpid(child_pid, 0)
    assert os.waitstatus_to_exitcode(status) == 0
    return result == b"1"


__all__ = [
    "FORK_WARNING",
    "NEEDS_SETTRACE_SAFE_AUDIT_HOOKS",
    "cleanup_child_hold",
    "error_details",
    "fork_descriptor_probe",
    "probe_lock",
]
