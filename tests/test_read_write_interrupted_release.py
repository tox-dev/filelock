from __future__ import annotations

from typing import TYPE_CHECKING, Final, Literal, TypeVar

import pytest

pytest.importorskip("sqlite3")

import sqlite3

from filelock import ReadWriteLock, Timeout
from filelock._read_write import _ForkSafeConnection

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from pytest_mock import MockerFixture


_T = TypeVar("_T")


class _Interrupt(BaseException):
    """Stands in for an exception a signal handler raises, without pytest treating it as a KeyboardInterrupt."""


def _interrupt_once(real: Callable[[_T], None], *, before: bool) -> Callable[[_T], None]:
    pending = True

    def call(instance: _T) -> None:
        nonlocal pending
        if pending and before:
            pending = False
            raise _Interrupt
        real(instance)
        if pending:
            pending = False
            raise _Interrupt

    return call


@pytest.mark.parametrize("mode", [pytest.param("read", id="read"), pytest.param("write", id="write")])
@pytest.mark.parametrize(
    "step",
    [
        pytest.param("rollback", id="after-rollback"),
        pytest.param("close", id="after-close"),
        pytest.param("level-reset", id="at-level-reset"),
    ],
)
def test_read_write_lock_after_an_interrupted_release_locks_again(
    tmp_path: Path,
    mocker: MockerFixture,
    mode: Literal["read", "write"],
    step: Literal["rollback", "close", "level-reset"],
) -> None:
    lock_file: Final = str(tmp_path / "lock.db")
    lock: Final = ReadWriteLock(lock_file, is_singleton=False)
    acquire: Final = lock.acquire_read if mode == "read" else lock.acquire_write
    acquire()
    if step == "level-reset":
        reset = _interrupt_once(ReadWriteLock._clear_lock_state, before=True)
        mocker.patch.object(ReadWriteLock, "_clear_lock_state", new=reset)
    else:
        real = sqlite3.Connection.rollback if step == "rollback" else _ForkSafeConnection.close
        mocker.patch.object(_ForkSafeConnection, step, new=_interrupt_once(real, before=False))
    with pytest.raises(_Interrupt):
        lock.release()

    acquire()

    contender: Final = ReadWriteLock(lock_file, is_singleton=False)
    with pytest.raises(Timeout):
        contender.acquire_write(blocking=False)
    lock.release()
    contender.close()
    lock.close()
