from __future__ import annotations

from ._marker import MarkerSoftFileLock, OwnerMode


class StrictSoftFileLock(MarkerSoftFileLock):
    """
    Fail-closed existence lock: a marker this process did not publish always means contention.

    :class:`SoftFileLock <filelock.SoftFileLock>` reclaims a marker whose holder it cannot find, and reclaims one by age
    when :attr:`~filelock.BaseFileLock.lifetime` is set. Both let a contender enter while the previous holder keeps
    running. This lock never reclaims. A malformed record, an unreadable record, an owner on another host, a dead PID
    and an old marker all read as held, so acquisition waits or times out instead of overlapping a holder that may still
    be alive.

    A crashed holder therefore leaves a marker that no contender removes. Clear it with
    :meth:`MarkerSoftFileLock.force_break <filelock.MarkerSoftFileLock.force_break>`, which states its own loss of
    guarantee, once an operator knows the holder is gone.

    The published record is protocol 2. A :class:`SoftFileLock <filelock.SoftFileLock>` contender reads it as malformed
    and evicts it, so the two classes do not exclude each other; run one contract across all contenders for a path.

    .. versionadded:: 3.30.0

    """

    _owner_mode: OwnerMode = "strict"

    #: Age-based expiry is what this lock exists to refuse, so it drops lifetime rather than honor it.
    _lifetime_supported: bool = False
    _lifetime_unsupported_reason: str = "a strict lock never reclaims a marker by age"

    def _try_break_stale_lock(self) -> None:
        """Leave the marker alone: under a strict contract every existing marker is a live holder."""


__all__ = [
    "StrictSoftFileLock",
]
