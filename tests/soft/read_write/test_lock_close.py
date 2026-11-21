from __future__ import annotations

from contextlib import nullcontext
from typing import TYPE_CHECKING, Final

import pytest

from filelock import SoftReadWriteLock
from filelock._soft._read_write._participant import Participant

from .lock_helpers import holders, make_lock, read_state, writer_gets_in

if TYPE_CHECKING:
    from collections.abc import Callable
    from contextlib import AbstractContextManager

    from pytest_mock import MockerFixture

pytestmark: Final[list[pytest.MarkDecorator]] = [
    pytest.mark.requires_hard_links,
    pytest.mark.usefixtures("clear_singletons"),
]


def test_close_is_idempotent(lock_file: str) -> None:
    lock = SoftReadWriteLock(lock_file)
    lock.acquire_write(timeout=2)
    lock.close()
    lock.close()
    assert writer_gets_in(lock_file)
    with pytest.raises(RuntimeError, match="has been closed"):
        lock.acquire_read(timeout=0)


def test_acquire_on_closed_raises(lock_file: str) -> None:
    lock = SoftReadWriteLock(lock_file)
    lock.close()
    with pytest.raises(RuntimeError, match="has been closed"):
        lock.acquire_read(timeout=1)
    with pytest.raises(RuntimeError, match="has been closed"):
        lock.acquire_write(timeout=1)


@pytest.mark.parametrize(
    "blocking",
    [
        pytest.param(lambda lock: lock.write_lock(timeout=2), id="while-blocked"),
        pytest.param(lambda _lock: nullcontext(), id="as-granted"),
    ],
)
def test_close_during_acquire_leaves_no_hold(
    lock_file: str, mocker: MockerFixture, blocking: Callable[[SoftReadWriteLock], AbstractContextManager[None]]
) -> None:
    blocker = make_lock(lock_file)
    victim = make_lock(lock_file)
    real_advance = Participant.advance

    def close_during(participant: Participant) -> bool:
        granted = real_advance(participant)
        victim.close()
        return granted

    mocker.patch.object(Participant, "advance", autospec=True, side_effect=close_during)
    try:
        with blocking(blocker), pytest.raises(RuntimeError, match="has been closed"):
            victim.acquire_write(timeout=2)
    finally:
        blocker.close()
    assert victim.generation is None
    assert read_state(lock_file).members == frozenset()
    assert holders(lock_file) == []


def test_close_while_polling_stops_the_wait(lock_file: str, mocker: MockerFixture) -> None:
    blocker = make_lock(lock_file)
    victim = make_lock(lock_file)
    real_advance = Participant.advance
    polls: list[bool] = []

    def close_on_second_poll(participant: Participant) -> bool:
        polls.append(True)
        if len(polls) == 2:
            victim.close()
        return real_advance(participant)

    mocker.patch.object(Participant, "advance", autospec=True, side_effect=close_on_second_poll)
    with blocker.write_lock(timeout=2), pytest.raises(RuntimeError, match="has been closed"):
        victim.acquire_write(timeout=2)
    assert (len(polls), victim.generation, holders(lock_file)) == (2, None, [])
