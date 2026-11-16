from __future__ import annotations

import os
import sys
from typing import TYPE_CHECKING, NoReturn, cast

import pytest

from tests.capability_marks import NEEDS_FORK
from tests.fork_helpers import fork_process

if TYPE_CHECKING:
    from collections.abc import Callable


@NEEDS_FORK
def test_fork_child_whose_callable_raises_exits_70(capfd: pytest.CaptureFixture[str]) -> None:  # pragma: win32 no cover
    def raising_child() -> NoReturn:
        msg = "child failure"
        raise RuntimeError(msg)

    _, status = os.waitpid(fork_process(raising_child), 0)

    assert os.waitstatus_to_exitcode(status) == 70
    assert "RuntimeError: child failure" in capfd.readouterr().err


@NEEDS_FORK
def test_fork_child_whose_callable_returns_exits_70() -> None:  # pragma: win32 no cover
    _, status = os.waitpid(fork_process(cast("Callable[[], NoReturn]", lambda: None)), 0)

    assert os.waitstatus_to_exitcode(status) == 70


@NEEDS_FORK
@pytest.mark.filterwarnings("ignore:.*multi-threaded, use of fork.*:DeprecationWarning")
def test_escaped_child_is_stopped_at_report() -> None:  # pragma: win32 no cover
    assert sys.platform != "win32"  # narrows os.fork for the type checker
    child_pid = os.fork()
    if child_pid == 0:  # pragma: forked child  # simplint: ignore[conditional-in-test]  # the child returns here
        return

    _, status = os.waitpid(child_pid, 0)

    assert os.waitstatus_to_exitcode(status) == 70
