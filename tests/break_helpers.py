from __future__ import annotations

import os
from typing import TYPE_CHECKING, Final

import pytest

import filelock._identity

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from pytest_mock import MockerFixture

#: How a stale break takes the marker: by the platform's own means, or as where no atomic exchange exists, which moves
#: a marker only on Windows, whose open markers refuse the rename.
BREAK_ROUTES: Final[pytest.MarkDecorator] = pytest.mark.parametrize(
    "break_route", [pytest.param("native", id="native"), pytest.param("no-exchange", id="no-exchange")]
)


def take_break_route(mocker: MockerFixture, route: str) -> None:
    """Make the stale break take *route*, as on a filesystem or runtime that offers only that much."""
    if route == "no-exchange":
        mocker.patch("filelock._util._rename_call", return_value=None)


def on_break_recheck(mocker: MockerFixture, lock: Path, action: Callable[[], None]) -> None:
    """Run *action* once, right after the stale break re-reads *lock* and before it takes the marker."""
    alive: Final = filelock._identity.process_alive
    lstat: Final = os.lstat
    state: list[str] = []

    def judged(pid: int) -> bool:
        # The owner check comes right before the break, whose first look at the path is the recheck.
        state.append("judged")
        return alive(pid)

    def lstat_then_act(path: str | os.PathLike[str], *, dir_fd: int | None = None) -> os.stat_result:
        found: Final = lstat(path, dir_fd=dir_fd)
        if state == ["judged"] and os.fspath(path) == os.fspath(lock):
            state.append("acted")
            action()
        return found

    mocker.patch("filelock._identity.process_alive", side_effect=judged)
    mocker.patch.object(os, "lstat", new=lstat_then_act)


__all__ = [
    "BREAK_ROUTES",
    "on_break_recheck",
    "take_break_route",
]
