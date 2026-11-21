from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    assert __spec__ is not None

__lazy_modules__ = ["inspect", "operator", "warnings", f"{__spec__.parent}._settings"]

import inspect
import time
import warnings
from dataclasses import dataclass, field, fields
from operator import attrgetter
from threading import local
from typing import NamedTuple

from ._settings import check_blocking, resolve_poll_interval, resolve_timeout

if TYPE_CHECKING:
    import contextlib
    from collections.abc import Callable, Hashable
    from concurrent.futures import Future
    from types import FrameType

    from filelock.internal.soft import LeaseClaim, StrictRelease


def on_this_stack(frame: FrameType | None) -> bool:
    """
    Detect re-entry by a signal handler on the interrupted thread.

    :param frame: frame that may have been interrupted
    :returns: whether the frame is active on this thread
    """
    if frame is None:
        return False
    caller = inspect.currentframe()
    while caller is not None and caller is not frame:
        caller = caller.f_back
    return caller is not None


@dataclass(eq=False, slots=True, kw_only=True)  # simplint: ignore[dataclass-frozen]  # release records its outcome here
class InFlight:
    frame: FrameType
    levels_before: int | None = None
    voided: bool = False
    rolled_back: bool = False


@dataclass  # simplint: ignore[dataclass-slots, dataclass-kw-only]  # base rejects slots; signature is public
class FileLockContext:  # simplint: ignore[too-many-attributes]  # its fields are public API
    """
    Hold state and mutable options for a lock or one of its threads.

    :param lock_file: lock path supplied at construction
    :param timeout: maximum wait in seconds, negative for unlimited
    :param mode: file creation permissions
    :param blocking: whether a contended acquire retries
    :param poll_interval: delay between attempts in seconds
    :param lifetime: age in seconds at which a soft marker expires
    :param lock_file_fd: descriptor of the held lock
    :param lock_file_fd_token: fork registry entry for the held descriptor
    :param lock_file_fd_identity: device and inode of the held descriptor
    :param pending_lock_file_fd: descriptor opened before the hold commits
    :param pending_lock_file_fd_identity: device and inode of the pending descriptor
    :param lock_counter: number of outstanding recursive acquisitions
    :param lock_file_key: holder's key in the deadlock registry
    :param lock_file_registry: acquiring thread's registry, retained for cross-thread release
    :param owner_claim_paths: published strict claims this holder may withdraw
    :param claim_deadline: monotonic deadline for Windows strict claim publication
    :param claim_withdrawal: completion of a withdrawal that outlived its acquire
    :param strict_release: completion of a Windows strict release with a deadline
    :param claim_descriptors: handles retained for deleting Windows claims
    :param claim_protection: handles pinning claim directories and files on Windows
    :param releasing: frame performing a release on this hold
    :param acquiring: frame performing the latest acquire on this hold
    :param claim_root: canonical lock path pinned against directory changes
    :param lease_claim: lease whose heartbeat belongs to this hold
    :param lock_generation: generation used to identify an interrupted release
    :param in_flight: acquisitions awaiting commit or rollback
    :param unlock_uncertain: whether the kernel may have unlocked before raising
    :ivar soft_fallback: whether this hold uses a marker after flock returned ENOSYS
    """

    lock_file: str
    timeout: float
    mode: int
    blocking: bool
    poll_interval: float
    lifetime: float | None = None
    soft_fallback: bool = field(default=False, init=False)
    lock_file_fd: int | None = None
    lock_file_fd_token: int | None = None
    lock_file_fd_identity: tuple[int, int] | None = None
    pending_lock_file_fd: int | None = None
    pending_lock_file_fd_identity: tuple[int, int] | None = None
    lock_counter: int = 0
    lock_file_key: Hashable | None = None
    # A different thread can release a shared lock. It needs the acquiring thread's registry.
    lock_file_registry: dict[Hashable, int] | None = None
    owner_claim_paths: tuple[str, ...] = ()
    claim_deadline: float | None = None
    claim_withdrawal: Future[tuple[list[str], list[BaseException]]] | None = None
    strict_release: StrictRelease | None = None
    claim_descriptors: dict[str, int] | None = None
    claim_protection: contextlib.ExitStack[bool | None] | None = None
    # A signal handler must not release a descriptor its interrupted frame already closed.
    releasing: FrameType | None = None
    acquiring: FrameType | None = None
    # A directory change during a wait or hold must not redirect the release.
    claim_root: str | None = None
    lease_claim: LeaseClaim | None = None
    lock_generation: int = field(default=0, kw_only=True)
    in_flight: list[InFlight] = field(default_factory=list, kw_only=True, repr=False)
    unlock_uncertain: bool = field(default=False, kw_only=True)

    def acquire_wait(
        self,
        timeout: float | None,
        poll_interval: float | None,
        poll_intervall: float | None,
        *,
        blocking: bool | None,
        cancel_check: Callable[[], bool] | None,
    ) -> AcquireWait:
        """
        Resolve per-call overrides against this thread's settings.

        :param timeout: maximum wait in seconds, negative for unlimited
        :param poll_interval: delay between attempts in seconds
        :param poll_intervall: deprecated spelling of poll_interval
        :param blocking: whether a contended acquire retries
        :param cancel_check: optional predicate for canceling a contended acquire
        :returns: validated options and the monotonic start time
        """
        timeout = self.timeout if timeout is None else timeout
        blocking = self.blocking if blocking is None else blocking

        check_blocking(blocking)

        if poll_intervall is not None:
            warnings.warn("use poll_interval instead of poll_intervall", DeprecationWarning, stacklevel=3)
            poll_interval = poll_intervall

        poll_interval = poll_interval if poll_interval is not None else self.poll_interval
        poll_interval = resolve_poll_interval(poll_interval)
        timeout = resolve_timeout(timeout, blocking=blocking)

        return AcquireWait(blocking, cancel_check, timeout, poll_interval, time.perf_counter())


class AcquireWait(NamedTuple):
    """
    Resolved wait policy shared by admission and backend polling.

    :param blocking: retry on contention
    :param cancel_check: caller's cancellation predicate, if supplied
    :param timeout: seconds from start, negative for an unlimited wait
    :param poll_interval: seconds between attempts
    :param start_time: monotonic start for both wait phases
    """

    blocking: bool
    cancel_check: Callable[[], bool] | None
    timeout: float
    poll_interval: float
    start_time: float


class ThreadLocalFileContext(FileLockContext, local):
    """
    Keep each thread's hold available for cleanup after that thread exits.

    :param lock_file: lock path supplied at construction
    :param timeout: maximum wait in seconds, negative for unlimited
    :param mode: file creation permissions
    :param blocking: whether a contended acquire retries
    :param poll_interval: delay between attempts in seconds
    :param lifetime: age in seconds at which a soft marker expires
    :ivar current: hold belonging to the calling thread
    """

    current: FileLockContext

    # threading.local runs __init__ again with these arguments the first time each thread touches the context.
    def __init__(  # ruff:ignore[too-many-arguments]  # mirrors the FileLockContext fields set at construction
        self,
        *,
        lock_file: str,
        timeout: float,
        mode: int,
        blocking: bool,
        poll_interval: float,
        lifetime: float | None = None,
    ) -> None:
        self.current = FileLockContext(
            lock_file=lock_file,
            timeout=timeout,
            mode=mode,
            blocking=blocking,
            poll_interval=poll_interval,
            lifetime=lifetime,
        )


def delegate_to_current(name: str) -> property:
    """
    Forward a context field to the calling thread's hold.

    :param name: name of a context field
    :returns: property bound to the calling thread's context
    """
    return property(
        attrgetter(f"current.{name}"),
        lambda context, value: setattr(context.current, name, value),
    )


for _field in fields(FileLockContext):
    setattr(ThreadLocalFileContext, _field.name, delegate_to_current(_field.name))


__all__ = [
    "AcquireWait",
    "FileLockContext",
    "InFlight",
    "ThreadLocalFileContext",
    "delegate_to_current",
    "on_this_stack",
]
