from __future__ import annotations

import asyncio
import contextlib
from concurrent.futures import Future as ConcurrentFuture
from typing import TYPE_CHECKING, Final

import pytest

from filelock._async import _AsyncTransitionGate
from tests.capability_marks import XFAIL_WITHOUT_COROUTINE_CANCELLATION

if TYPE_CHECKING:
    from pytest_mock import MockerFixture


@pytest.mark.asyncio
async def test_hold_waits_for_a_predecessor_before_entering() -> None:
    gate = _AsyncTransitionGate()
    order: list[str] = []
    first_holding = asyncio.Event()
    release_first = asyncio.Event()

    async def first() -> None:
        async with gate.hold():
            first_holding.set()
            order.append("first-in")
            await release_first.wait()
        order.append("first-out")

    async def second() -> None:
        async with gate.hold():
            order.append("second-in")

    first_task = asyncio.create_task(first())
    await first_holding.wait()
    second_task = asyncio.create_task(second())
    await asyncio.sleep(0.05)  # second parks on the predecessor instead of entering
    assert order == ["first-in"]

    release_first.set()
    await asyncio.gather(first_task, second_task)
    assert order == ["first-in", "first-out", "second-in"]


@pytest.mark.asyncio
@XFAIL_WITHOUT_COROUTINE_CANCELLATION
async def test_hold_canceled_while_waiting_takes_its_ticket_out() -> None:
    gate = _AsyncTransitionGate()
    first_holding = asyncio.Event()
    release_first = asyncio.Event()

    async def first() -> None:
        async with gate.hold():
            first_holding.set()
            await release_first.wait()

    async def second() -> None:
        async with gate.hold():  # never reached: canceled while waiting on first
            pass  # pragma: no cover  # the hold body never runs: the waiter is canceled while parked in hold()

    first_task = asyncio.create_task(first())
    await first_holding.wait()
    second_task = asyncio.create_task(second())
    await asyncio.sleep(0.05)  # second is parked on the predecessor
    second_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await second_task

    release_first.set()
    await first_task

    # The canceled waiter took its ticket out of the queue; a fresh holder must then enter without waiting on a ticket
    # that no coroutine will ever leave.
    entered = asyncio.Event()

    async def third() -> None:
        async with gate.hold():
            entered.set()

    await asyncio.wait_for(third(), timeout=1)
    assert entered.is_set()


@pytest.mark.asyncio
async def test_hold_interrupted_while_waiting_takes_its_ticket_out() -> None:
    gate = _AsyncTransitionGate()
    first_holding = asyncio.Event()
    release_first = asyncio.Event()

    async def first() -> None:
        async with gate.hold():
            first_holding.set()
            await release_first.wait()

    first_task = asyncio.create_task(first())
    await first_holding.wait()
    parked = gate.hold().__aenter__()  # ruff:ignore[unnecessary-dunder-call]  # step the entry by hand to throw into it
    parked.send(None)  # runs up to the wait on the predecessor
    with pytest.raises(_Interrupt):
        parked.throw(_Interrupt())
    release_first.set()
    await first_task

    async def third() -> None:
        async with gate.hold():
            pass

    await asyncio.wait_for(third(), timeout=1)


@pytest.mark.parametrize("joined", [pytest.param(True, id="after-join"), pytest.param(False, id="before-join")])
@pytest.mark.parametrize("entry", [pytest.param("hold", id="hold"), pytest.param("hold_for_acquire", id="acquire")])
@pytest.mark.asyncio
async def test_hold_interrupted_while_joining_leaves_the_gate_free(
    mocker: MockerFixture, entry: str, *, joined: bool
) -> None:
    gate: Final = _AsyncTransitionGate()
    real_join: Final = _AsyncTransitionGate._join
    interrupted = False

    def join_then_interrupt(instance: _AsyncTransitionGate, ticket: ConcurrentFuture[None]) -> None:
        nonlocal interrupted
        if not interrupted:
            interrupted = True
            if joined:
                real_join(instance, ticket)
            raise _Interrupt
        real_join(instance, ticket)

    mocker.patch.object(_AsyncTransitionGate, "_join", autospec=True, side_effect=join_then_interrupt)

    def hold() -> contextlib.AbstractAsyncContextManager[None]:
        if entry == "hold":
            return gate.hold()
        return gate.hold_for_acquire(blocking=True, cancel_check=None, deadline=None, poll_interval=0.01)

    with pytest.raises(_Interrupt):
        await contextlib.AsyncExitStack().enter_async_context(hold())
    entered = False
    async with asyncio.timeout(5), hold():
        entered = True

    assert entered


@pytest.mark.asyncio
async def test_parked_caller_settles_a_head_whose_hand_off_was_lost(mocker: MockerFixture) -> None:
    gate: Final = _AsyncTransitionGate()
    real_settle: Final = _AsyncTransitionGate._settle_head_locked
    calls = 0

    def lose_first_hand_off(instance: _AsyncTransitionGate) -> None:
        nonlocal calls
        calls += 1
        if calls > 1:
            real_settle(instance)

    first_holding, release_first, second_entered = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def first() -> None:
        async with gate.hold():
            first_holding.set()
            await release_first.wait()

    async def second() -> None:
        async with gate.hold():
            second_entered.set()

    first_task = asyncio.create_task(first())
    await first_holding.wait()
    second_task = asyncio.create_task(second())
    await asyncio.sleep(0)
    mocker.patch.object(_AsyncTransitionGate, "_settle_head_locked", autospec=True, side_effect=lose_first_hand_off)

    release_first.set()
    async with asyncio.timeout(1):
        await second_entered.wait()
    await asyncio.gather(first_task, second_task)


@pytest.mark.asyncio
@pytest.mark.timeout(5)
async def test_wait_for_turn_returns_immediately_when_already_done() -> None:
    ticket: ConcurrentFuture[None] = ConcurrentFuture()
    ticket.set_result(None)

    await _AsyncTransitionGate()._wait_for_turn(
        ticket, blocking=True, cancel_check=None, deadline=None, poll_interval=0.01
    )

    assert ticket.done()


class _Interrupt(BaseException):
    """Stands in for a KeyboardInterrupt delivered while a waiter is parked, without pytest treating it as one."""
