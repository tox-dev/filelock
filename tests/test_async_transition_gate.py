from __future__ import annotations

import asyncio
from concurrent.futures import Future as ConcurrentFuture

import pytest

from filelock._async import _AsyncTransitionGate
from tests.capability_marks import XFAIL_WITHOUT_COROUTINE_CANCELLATION


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
async def test_hold_canceled_while_waiting_lets_the_predecessor_free_the_ticket() -> None:
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

    # The canceled waiter registered a callback so the predecessor frees its abandoned ticket; a fresh holder must
    # then enter without waiting on a tail that no coroutine will ever leave.
    entered = asyncio.Event()

    async def third() -> None:
        async with gate.hold():
            entered.set()

    await asyncio.wait_for(third(), timeout=1)
    assert entered.is_set()


@pytest.mark.asyncio
async def test_hold_interrupted_while_waiting_lets_the_predecessor_free_the_ticket() -> None:
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


@pytest.mark.asyncio
@pytest.mark.timeout(5)
async def test_wait_for_predecessor_returns_immediately_when_already_done() -> None:
    predecessor: ConcurrentFuture[None] = ConcurrentFuture()
    predecessor.set_result(None)

    await _AsyncTransitionGate._wait_for_predecessor(
        predecessor, blocking=True, cancel_check=None, deadline=None, poll_interval=0.01
    )

    assert predecessor.done()


class _Interrupt(BaseException):
    """Stands in for a KeyboardInterrupt delivered while a waiter is parked, without pytest treating it as one."""
