"""Cross-process and cross-host reader/writer lock over a generation log of immutable snapshots."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from importlib.machinery import ModuleSpec

__spec__ = cast("ModuleSpec", __spec__)

__lazy_modules__ = [
    "filelock",
    "filelock.internal.async_lock",
    "functools",
    f"{__spec__.parent}._generations",
    f"{__spec__.parent}._heartbeat",
    f"{__spec__.parent}._hold",
    f"{__spec__.parent}._participant",
    f"{__spec__.parent}._storage",
    f"{__spec__.parent}._sweep",
]

import atexit
import os
import threading
import time
from contextlib import contextmanager, suppress
from functools import partial
from typing import TYPE_CHECKING, ClassVar, Final
from weakref import WeakValueDictionary

from filelock import LeaseCompromise
from filelock.internal.async_lock import (
    TASKS_THREAD_ID,
    retire_task_holds_at_exit,
)
from filelock.internal.core import (
    AcquireReturnProxy,
    ForkClassResettable,
    ForkResettable,
    ReadWriteBackend,
    Timeout,
    acquire_thread_lock,
    canonical,
    ensure_current_process,
    override,
    register_fork_class,
    register_fork_object,
    resolve_read_write_timeout,
)

from ._generations import GenerationLog
from ._heartbeat import HeartbeatThread, lease_clock, validate_intervals
from ._hold import Hold, Locks, Losses
from ._participant import Mode, Participant
from ._singleton import SoftRWMeta
from ._storage import OsFiles
from ._sweep import Ledger

if TYPE_CHECKING:
    from collections.abc import Callable, Generator

    from filelock import CompromiseReason

_ALL_INSTANCES: Final[WeakValueDictionary[int, SoftReadWriteLock]] = WeakValueDictionary()
_ALL_INSTANCES_LOCK: threading.Lock = threading.Lock()


class SoftReadWriteLock(ReadWriteBackend, ForkResettable, ForkClassResettable, metaclass=SoftRWMeta):
    """
    Reader/writer lock for shared filesystems such as NFS and Lustre.

    A waiting writer excludes new readers. Write holds are thread-owned. Upgrading or downgrading a hold raises
    :class:`RuntimeError`. A forked child must create its own lock.

    Each generation is published by atomic hard link. A contender may evict a holder whose record stays unchanged
    for ``stale_threshold`` on its own clock. Use :attr:`generation` to fence resumed writers. Lease time includes
    host suspension on macOS and platforms with ``CLOCK_BOOTTIME``. Owner-only permissions protect records from
    other accounts. Privileged users and tenants sharing a UID can modify records.

    :param lock_file: lock path. The protocol directory is ``<lock_file>.rw``.
    :param timeout: maximum wait in seconds, or ``-1`` to wait indefinitely
    :param blocking: if ``False``, raise :class:`~filelock.Timeout` immediately on contention
    :param is_singleton: if ``True``, reuse existing instances for the same canonical path

    :param heartbeat_interval: seconds between heartbeat refreshes (default 30)
    :param stale_threshold: seconds a holder record may go unchanged before eviction (default three heartbeat intervals)
    :param poll_interval: seconds between attempts under contention (default 0.25)
    :param on_compromise: heartbeat callback for eviction or a refresh failure long enough to allow eviction

    .. versionadded:: 3.27.0

    .. versionchanged:: 3.33.0

        The generation-log format requires all holders sharing a path to use 3.33.0 or later.

    """

    _instances: ClassVar[WeakValueDictionary[str, SoftReadWriteLock]] = WeakValueDictionary()
    _instances_lock = threading.RLock()
    _instances_under_construction: ClassVar[set[str]] = set()

    @override
    def __init_subclass__(cls, **kwargs: object) -> None:
        super().__init_subclass__(**kwargs)
        cls._reset_singletons()
        register_fork_class(cls)

    def __init__(  # ruff:ignore[too-many-arguments]  # public constructor: one parameter per documented lock option
        self,
        lock_file: str | os.PathLike[str],
        timeout: float = -1,
        *,
        blocking: bool = True,
        is_singleton: bool = True,  # ruff:ignore[unused-method-argument]  # consumed by SoftRWMeta.__call__
        heartbeat_interval: float = 30.0,
        stale_threshold: float | None = None,
        poll_interval: float = 0.25,
        on_compromise: Callable[[LeaseCompromise], None] | None = None,
    ) -> None:
        self._creator_pid = os.getpid()
        heartbeat_interval, stale_threshold, poll_interval = validate_intervals(
            heartbeat_interval, stale_threshold, poll_interval
        )

        self.lock_file: str = os.fspath(lock_file)
        self.timeout: float = timeout
        self.blocking: bool = blocking
        self.heartbeat_interval: float = heartbeat_interval
        self.stale_threshold: float = stale_threshold
        self.poll_interval: float = poll_interval

        # A later chdir must not redirect the hold.
        root = f"{canonical(self.lock_file)}.rw"
        files = OsFiles(self.lock_file)
        self._new_participant: Final = partial(
            Participant,
            files,
            root,
            ledger=Ledger(time.monotonic),
            log=GenerationLog(files, self.lock_file, root),
        )
        self._losses = Losses(callback=on_compromise)
        self._locks = Locks(internal=threading.Lock(), transaction=threading.Lock())
        self._hold: Hold | None = None
        self._closed: bool = False

        with _ALL_INSTANCES_LOCK:
            _ALL_INSTANCES[id(self)] = self
        register_fork_object(self)

    @classmethod
    def get_lock(
        cls,
        lock_file: str | os.PathLike[str],
        timeout: float = -1,
        *,
        blocking: bool = True,
    ) -> SoftReadWriteLock:
        """
        Return the singleton for *lock_file*.

        :param lock_file: lock path
        :param timeout: wait limit in seconds, or ``-1`` to wait indefinitely
        :param blocking: whether to wait on contention
        :returns: the singleton lock
        :raises ValueError: the cached instance uses different timeout or blocking settings
        """
        return cls(lock_file, timeout, blocking=blocking)

    @property
    def generation(self) -> int | None:
        """
        The generation at which the current hold was granted, or ``None`` when no lock is held.

        A protected resource can reject writes with an older generation. Removing the log resets generations to 1.

        .. versionadded:: 3.33.0

        """
        with self._locks.internal:
            return None if self._hold is None else self._hold.participant.generation

    @property
    def compromise(self) -> LeaseCompromise | None:
        """
        How the current hold was lost, or ``None`` while it stands.

        Stop using the resource and release a compromised hold before acquiring again.

        .. versionadded:: 3.33.0

        """
        with self._locks.internal:
            return self._losses.latest

    @classmethod
    @override
    def _reset_class_after_fork(cls) -> None:  # pragma: forked child
        global _ALL_INSTANCES_LOCK  # ruff:ignore[global-statement]  # rebinds the module lock to a fresh one in the fork child
        _ALL_INSTANCES_LOCK = threading.Lock()
        cls._reset_singletons()

    @contextmanager
    def read_lock(self, timeout: float | None = None, *, blocking: bool | None = None) -> Generator[None]:
        """
        Hold a shared lock until the context exits.

        See :meth:`acquire_read` for timeouts, reentrancy and errors.

        :param timeout: wait limit in seconds, or ``None`` for the instance default
        :param blocking: wait on contention, or ``None`` for the instance default
        :yields: while the shared lock is held
        """
        self.acquire_read(timeout, blocking=blocking)
        try:
            yield
        finally:
            self.release()

    def acquire_read(self, timeout: float | None = None, *, blocking: bool | None = None) -> AcquireReturnProxy:
        """
        Acquire a shared read lock. Reentry adds one level.

        :param timeout: wait limit in seconds, ``-1`` to wait indefinitely, or ``None`` for the instance default
        :param blocking: wait on contention, or ``None`` for the instance default
        :returns: a context manager that releases one level
        :raises RuntimeError: the instance is closed, inherited, compromised, or holds a write lock
        :raises Timeout: the lock remains unavailable
        :raises TypeError: invalid *blocking* or *timeout* type, including boolean timeouts
        :raises ValueError: a blocking timeout is NaN or negative other than ``-1``
        :raises SoftFileLockProtocolError: the snapshot is unreadable or the filesystem refuses hard links
        """
        return self._acquire("read", timeout, blocking=blocking)

    @override
    def _acquire(
        self,
        mode: Mode,
        timeout: float | None,
        *,
        blocking: bool | None,
        holder: int | None = None,
        cancel_check: Callable[[], bool] | None = None,
    ) -> AcquireReturnProxy:
        ensure_current_process()
        if self._creator_pid != os.getpid():  # pragma: forked child
            msg = f"SoftReadWriteLock on {self.lock_file} was invalidated by fork(); construct a new instance"
            raise RuntimeError(msg)
        timeout = self.timeout if timeout is None else timeout
        blocking = self.blocking if blocking is None else blocking
        holder = threading.get_ident() if holder is None else holder

        with self._locks.internal:
            if self._closed:
                raise self._closed_error()
            timeout = resolve_read_write_timeout(timeout, blocking=blocking)
            if (hold := self._hold) is not None:
                return hold.take_level(self, mode, holder, self._losses.latest)

        deadline = None if timeout == -1 else time.perf_counter() + timeout
        if not acquire_thread_lock(
            self._locks.transaction,
            blocking=blocking,
            timeout=timeout,
            poll_interval=self.poll_interval,
            cancel_check=cancel_check,
        ):
            raise Timeout(self.lock_file) from None
        try:
            return self._do_acquire_inner(mode, deadline, holder, blocking=blocking, cancel_check=cancel_check)
        finally:
            self._locks.transaction.release()

    def _closed_error(self) -> RuntimeError:
        return RuntimeError(f"SoftReadWriteLock on {self.lock_file} has been closed")

    def _do_acquire_inner(
        self,
        mode: Mode,
        deadline: float | None,
        holder: int,
        *,
        blocking: bool,
        cancel_check: Callable[[], bool] | None,
    ) -> AcquireReturnProxy:
        with self._locks.internal:
            if (hold := self._hold) is not None:
                return hold.take_level(self, mode, holder, self._losses.latest)
        participant = self._new_participant(
            mode, stale_threshold=self.stale_threshold, clock=time.monotonic, lease_clock=lease_clock
        )
        participant.publish()
        try:
            self._wait_for(participant.advance, deadline=deadline, blocking=blocking, cancel_check=cancel_check)
        except BaseException:
            # A queued writer excludes new readers until it leaves.
            with suppress(OSError):
                participant.leave()
            raise
        self._start_hold(participant, holder)
        return AcquireReturnProxy(lock=self)

    def _wait_for(
        self,
        predicate: Callable[[], bool],
        *,
        deadline: float | None,
        blocking: bool,
        cancel_check: Callable[[], bool] | None,
    ) -> None:
        while not predicate():
            if self._closed:
                raise self._closed_error()
            now = time.perf_counter()
            if not blocking or (deadline is not None and now >= deadline):
                raise Timeout(self.lock_file)
            if cancel_check is not None and cancel_check():
                raise Timeout(self.lock_file)
            sleep_for = self.poll_interval
            if deadline is not None:
                sleep_for = min(sleep_for, max(deadline - now, 0.0))
            time.sleep(sleep_for)

    def _start_hold(self, participant: Participant, holder: int) -> None:
        stop_event = threading.Event()
        hold = Hold(
            level=1,
            mode=participant.mode,
            write_thread_id=holder if participant.mode == "write" else None,
            read_depths={holder: 1} if participant.mode == "read" else {},
            participant=participant,
            heartbeat_thread=HeartbeatThread(
                refresh=lambda: hold.refresh(self, self._report_compromise),
                interval=self.heartbeat_interval,
                stop_event=stop_event,
                name=f"filelock-heartbeat-{id(self):x}",
            ),
            heartbeat_stop=stop_event,
            last_refresh=lease_clock(),
        )
        # Release can only join the heartbeat once the thread has started.
        start_error: BaseException | None = None
        with self._locks.internal:
            if self._closed:
                start_error = self._closed_error()
            else:
                self._hold = hold
                self._losses.latest = None
                try:
                    hold.heartbeat_thread.start()
                except BaseException as error:  # ruff:ignore[blind-except]  # clear the slot below and re-raise
                    self._hold = None
                    start_error = error
        if start_error is not None:
            participant.leave()
            raise start_error

    def _report_compromise(self, hold: Hold, reason: CompromiseReason, error: Exception | None) -> None:
        # A late heartbeat can finish after another acquisition.
        with self._locks.internal:
            if self._hold is not hold:
                return
            compromise = LeaseCompromise(
                lock_file=self.lock_file, token=hold.participant.token, reason=reason, error=error
            )
            self._losses.latest = compromise
        if self._losses.callback is not None:
            self._losses.callback(compromise)

    def release(self, *, force: bool = False) -> None:
        """
        Release one level. The final release withdraws the participant. A forked child preserves the parent's hold.

        :param force: if ``True``, release the lock completely regardless of the current lock level

        :raises RuntimeError: the caller holds no level and *force* is ``False``
        :raises SoftFileLockProtocolError: if peers keep winning generation updates until the holder's stale threshold

        """
        ensure_current_process()
        if self._creator_pid != os.getpid():  # pragma: forked child
            return
        self._release(threading.get_ident(), force=force)

    def _release(self, thread_id: int, *, force: bool, at_exit: bool = False) -> None:
        with self._locks.internal:
            hold = self._hold
            if hold is None:
                if force:
                    return
                msg = f"Cannot release a lock on {self.lock_file} (lock id: {id(self)}) that is not held"
                raise RuntimeError(msg)
            if at_exit:
                owners = hold.read_depths if hold.write_thread_id is None else {hold.write_thread_id}
                if any(thread.ident in owners and thread.ident != thread_id for thread in threading.enumerate()):
                    return
                if TASKS_THREAD_ID in owners and not retire_task_holds_at_exit(self):
                    return
            if force:
                hold.level = 0
            else:
                hold.leave_one_level(self, thread_id)
            if hold.level > 0:
                return
            self._hold = None

        hold.withdraw(self.heartbeat_interval)

    @contextmanager
    def write_lock(self, timeout: float | None = None, *, blocking: bool | None = None) -> Generator[None]:
        """
        Hold an exclusive lock until the context exits.

        See :meth:`acquire_write` for timeouts, thread ownership and errors.

        :param timeout: wait limit in seconds, or ``None`` for the instance default
        :param blocking: wait on contention, or ``None`` for the instance default
        :yields: while the exclusive lock is held
        """
        self.acquire_write(timeout, blocking=blocking)
        try:
            yield
        finally:
            self.release()

    def acquire_write(self, timeout: float | None = None, *, blocking: bool | None = None) -> AcquireReturnProxy:
        """
        Acquire a thread-owned write lock. Reentry adds one level.

        :param timeout: wait limit in seconds, ``-1`` to wait indefinitely, or ``None`` for the instance default
        :param blocking: wait on contention, or ``None`` for the instance default
        :returns: a context manager that releases one level
        :raises RuntimeError: the instance is closed, inherited, compromised, or held for reading or by another writer
        :raises Timeout: the lock remains unavailable
        :raises TypeError: invalid *blocking* or *timeout* type, including boolean timeouts
        :raises ValueError: a blocking timeout is NaN or negative other than ``-1``
        :raises SoftFileLockProtocolError: the snapshot is unreadable or the filesystem refuses hard links
        """
        return self._acquire("write", timeout, blocking=blocking)

    @override
    def close(self) -> None:
        """
        Release any held lock and mark the instance closed.

        Pending and future acquires raise :class:`RuntimeError`. Closing again, including in a forked child, is safe.
        """
        ensure_current_process()
        if self._creator_pid != os.getpid():  # pragma: forked child
            return
        # An acquire already polling must observe close before publishing its hold.
        with self._locks.internal:
            self._closed = True
        self.release(force=True)

    @override
    def _release_level_of(self, holder: int) -> None:
        self._release(holder, force=False)

    @override
    def _holds_level_of(self, holder: int) -> bool:
        # The event loop must not block behind a release on another thread.
        return (hold := self._hold) is not None and (holder == hold.write_thread_id or holder in hold.read_depths)

    @override
    def _reset_after_fork_in_child(self) -> None:  # pragma: forked child
        self._creator_pid = -1
        self._locks = Locks(internal=threading.Lock(), transaction=threading.Lock())
        self._hold = None

    @staticmethod
    @atexit.register
    def _cleanup_all_instances() -> None:
        for instance in list(_ALL_INSTANCES.values()):
            with suppress(Exception):
                SoftReadWriteLock._release(instance, threading.get_ident(), force=True, at_exit=True)


register_fork_class(SoftReadWriteLock)


__all__ = [
    "SoftReadWriteLock",
]
