from __future__ import annotations

import os
from typing import TYPE_CHECKING, Final, NoReturn, cast

from coverage import Coverage, process_startup

if TYPE_CHECKING:
    from collections.abc import Callable
    from typing import Protocol

    class _RegisterAtFork(Protocol):
        def __call__(self, *, after_in_child: Callable[[], None] | None = None) -> None: ...


_FORK: Final[Callable[[], int] | None] = cast(
    "Callable[[], int] | None",
    getattr(os, "fork", None),  # fork is absent from Windows at runtime and in its type interface
)
_REGISTER_AT_FORK: Final[_RegisterAtFork | None] = cast(
    "_RegisterAtFork | None",
    getattr(  # register_at_fork is absent from Windows at runtime and in its type interface
        os,
        "register_at_fork",
        None,
    ),
)


def fork_process(child: Callable[[], NoReturn] | None = None) -> int:  # pragma: win32 no cover
    if _FORK is None:  # pragma: no cover - platform without os.fork
        msg = "os.fork is unavailable"
        raise RuntimeError(msg)
    child_pid = _FORK()
    if child_pid == 0 and child is not None:  # pragma: win32 no cover
        child()  # pragma: no cover - child coverage starts after fork returns to this frame
    return child_pid


def exit_child(status: int) -> NoReturn:
    try:
        if (coverage := Coverage.current()) is not None:
            coverage.save()
    finally:
        # A coverage write failure must not return the fork child to pytest.
        os._exit(status)


def _restart_coverage_after_fork() -> None:  # pragma: win32 no cover
    if (coverage := Coverage.current()) is not None:  # pragma: win32 no cover
        coverage.stop()
    process_startup(force=True, slug="fork")  # pragma: no cover - coverage is stopped until this call returns


if _REGISTER_AT_FORK is not None:  # pragma: win32 no cover
    _REGISTER_AT_FORK(after_in_child=_restart_coverage_after_fork)


__all__ = ["exit_child", "fork_process"]
