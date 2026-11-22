####################
 Lock context types
####################

:class:`~filelock.FileLockContext` stores the settings and ownership state for an exclusive lock. Its async subclass
adds the executor, event loop and transition gate.

.. autoclass:: filelock.FileLockContext
    :members:

Acquisition
===========

:meth:`~filelock.FileLockContext.acquire_wait` resolves the wait settings for an attempt.
The context's ``in_flight`` list tracks attempts that an interrupt may undo.

.. autoclass:: filelock.AcquireWait
    :members: blocking, cancel_check, timeout, poll_interval, start_time

.. autoclass:: filelock.InFlight
    :members: frame, levels_before, voided, rolled_back

Transitions
===========

The context's ``transition_gate`` serializes competing acquire and release operations.

.. autoclass:: filelock.asyncio.AsyncTransitionGate
    :members: hold, hold_for_acquire

.. autoexception:: filelock.asyncio.AsyncTransitionUnavailableError

Soft lock state
===============

A lease records its current claim in ``lease_claim``. A Windows strict lock uses ``strict_release`` to track a
timed release that is still running.

.. autoclass:: filelock.LeaseClaim
    :members: token, compromise, heartbeat

.. autoclass:: filelock.LeaseHeartbeat
    :members: thread, stop

.. autoclass:: filelock.StrictRelease
    :members: guard, worker
