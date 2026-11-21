from __future__ import annotations

from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from importlib.machinery import ModuleSpec

__spec__ = cast("ModuleSpec", __spec__)

__lazy_modules__ = ["filelock", "threading", f"{__spec__.parent}._heartbeat"]

import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING

from filelock import AcquireReturnProxy

from ._heartbeat import lease_clock

if TYPE_CHECKING:
    from collections.abc import Callable

    from filelock import CompromiseReason, LeaseCompromise

    from ._heartbeat import HeartbeatThread
    from ._lock import SoftReadWriteLock
    from ._participant import Mode, Participant


@dataclass(slots=True, kw_only=True)
class Losses:
    callback: Callable[[LeaseCompromise], None] | None
    latest: LeaseCompromise | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class Locks:
    internal: threading.Lock
    transaction: threading.Lock


@dataclass(slots=True, kw_only=True)
class Hold:
    level: int
    mode: Mode
    write_thread_id: int | None
    read_depths: dict[int, int]
    participant: Participant
    heartbeat_thread: HeartbeatThread
    heartbeat_stop: threading.Event
    last_refresh: float

    def refresh(
        self,
        lock: SoftReadWriteLock,
        report: Callable[[Hold, CompromiseReason, Exception | None], None],
    ) -> bool:
        # Retry transient filesystem errors until a peer could evict this hold before its next refresh.
        try:
            outcome, error = self.participant.heartbeat()
        except Exception as failure:  # ruff:ignore[blind-except]  # the thread would end with the hold still published
            report(self, "refresh-failed", failure)
            return False
        if outcome == "ok":
            self.last_refresh = lease_clock()
            return True
        if outcome == "lost":
            report(self, "evicted", None)
            return False
        if lease_clock() - self.last_refresh >= lock.stale_threshold - lock.heartbeat_interval:
            report(self, "refresh-failed", error)
            return False
        return True

    def take_level(
        self, lock: SoftReadWriteLock, mode: Mode, holder: int, compromise: LeaseCompromise | None
    ) -> AcquireReturnProxy[SoftReadWriteLock]:
        if self.mode != mode:
            opposite = "write" if mode == "read" else "read"
            direction = "downgrade" if mode == "read" else "upgrade"
            msg = (
                f"Cannot acquire {mode} lock on {lock.lock_file} (lock id: {id(lock)}): "
                f"already holding a {opposite} lock ({direction} not allowed)"
            )
            raise RuntimeError(msg)
        if mode == "write" and holder != self.write_thread_id:
            msg = (
                f"Cannot acquire write lock on {lock.lock_file} (lock id: {id(lock)}) "
                f"from thread {holder} while it is held by thread {self.write_thread_id}"
            )
            raise RuntimeError(msg)
        # A peer can take the lock after the heartbeat reports a lost claim.
        if compromise is not None:
            raise lost_hold_error(lock.lock_file, mode, compromise)
        if mode == "read":
            self.read_depths[holder] = self.read_depths.get(holder, 0) + 1
        self.level += 1
        return AcquireReturnProxy(lock=lock)

    def leave_one_level(self, lock: SoftReadWriteLock, thread_id: int) -> None:
        self._check_owner(lock, thread_id)
        self.level -= 1
        if self.mode == "read":
            # The heartbeat callback may release a level on behalf of a reader.
            owner = thread_id if thread_id in self.read_depths else next(iter(self.read_depths))
            if depth := self.read_depths.pop(owner) - 1:
                self.read_depths[owner] = depth

    def _check_owner(self, lock: SoftReadWriteLock, thread_id: int) -> None:
        # The hold's own heartbeat thread may release it, since on_compromise runs there.
        if thread_id == self.heartbeat_thread.ident:
            return
        if self.mode == "write" and thread_id != self.write_thread_id:
            msg = (
                f"Cannot release write lock on {lock.lock_file} (lock id: {id(lock)}) "
                f"from thread {thread_id} while it is held by thread {self.write_thread_id}"
            )
            raise RuntimeError(msg)
        if self.mode == "read" and thread_id not in self.read_depths:
            msg = (
                f"Cannot release read lock on {lock.lock_file} (lock id: {id(lock)}) "
                f"from thread {thread_id}, which does not hold it"
            )
            raise RuntimeError(msg)

    def withdraw(self, heartbeat_interval: float) -> None:
        # A compromise callback can release its own hold from the heartbeat thread.
        self.heartbeat_stop.set()
        try:
            if self.heartbeat_thread is not threading.current_thread():
                self.heartbeat_thread.join(timeout=min(heartbeat_interval + 1.0, threading.TIMEOUT_MAX))
        finally:
            self.participant.leave()


def lost_hold_error(lock_file: str, mode: Mode, compromise: LeaseCompromise) -> RuntimeError:
    return RuntimeError(
        f"Cannot acquire {mode} lock on {lock_file}: the current hold was lost ({compromise.reason}), "
        "release it before acquiring again"
    )


__all__ = [
    "Hold",
    "Locks",
    "Losses",
    "lost_hold_error",
]
