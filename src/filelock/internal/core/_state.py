from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    assert __spec__ is not None

__lazy_modules__ = ["threading", "warnings", f"{__spec__.parent}._context", f"{__spec__.parent}._settings"]

import os
import warnings
from abc import ABC
from threading import RLock, Thread, current_thread
from typing import Final

from ._context import FileLockContext, ThreadLocalFileContext, on_this_stack
from ._fork import (
    HAS_REGISTER_AT_FORK,
    ForkClassResettable,
    ForkDescriptorOwner,
    ForkResettable,
    ensure_current_process,
    fork_transition,
    record_owned_descriptor,
    register_owned_descriptor,
    unregister_owned_descriptor,
)
from ._settings import (
    UNSET_FILE_MODE,
    CloseErrorPolicy,
    ContextErrorPolicy,
    LockSettings,
    check_blocking,
    resolve_lifetime,
    resolve_poll_interval,
    seconds,
)
from ._typing import override

if TYPE_CHECKING:
    from collections.abc import Callable


class LockState(ForkResettable, ForkDescriptorOwner, ForkClassResettable, ABC):
    _lifetime_supported: bool = False
    _lifetime_replacements: tuple[str, str] | None = None
    _lifetime_unsupported_reason: str = "a native OS lock cannot be broken safely by file age"
    _context: FileLockContext
    _settings: LockSettings
    _creator_pid: int
    _holder_id: int
    _transition_lock: RLock
    _thread_holds: dict[int, tuple[FileLockContext, Thread]]
    _thread_pending: dict[int, tuple[FileLockContext, Thread]]
    _flockless_roots: frozenset[str]
    _singleton_key: str | None = None
    _singleton_shared: bool = False
    _poll_backoff_cap: float = 0.0
    _deadlock_holder_desc: str = "FileLock instance in this thread"

    @property
    def is_locked(self) -> bool:
        """
        Whether this instance still records a hold. After a native unlock raises, this can remain ``True`` until an
        explicit ``release()`` retry, even if the kernel already released the lock.

        .. versionchanged:: 2.0.0

            This was previously a method and is now a property.

        """
        ensure_current_process()
        return self._context.lock_file_fd is not None

    @property
    def _lock_path(self) -> str:
        # Pinning prevents a directory change from redirecting an acquire or its matching release.
        return self._context.claim_root or self.lock_file

    @property
    def lock_file(self) -> str:
        """Path to the lock file."""
        return self._context.lock_file

    @property
    def is_singleton(self) -> bool:
        """
        A flag indicating if this lock is singleton or not.

        .. versionadded:: 3.13.0

        """
        return self._settings.is_singleton

    @property
    def context_error_policy(self) -> ContextErrorPolicy:
        """
        How a context manager reconciles a body failure with a release failure on exit.

        .. versionadded:: 3.30.0

        """
        return self._settings.context_error_policy

    @property
    def close_error_policy(self) -> CloseErrorPolicy:
        """
        What a lock does with an ``os.close`` failure after relinquishing descriptor ownership.

        .. versionadded:: 3.30.0

        """
        return self._settings.close_error_policy

    @property
    def fallback_to_soft(self) -> bool:
        """
        Whether a :class:`FileLock` falls back to :class:`SoftFileLock` when the filesystem lacks ``flock``.

        Only :class:`UnixFileLock` acts on it: when ``False`` an ``ENOSYS`` from ``flock`` propagates instead of
        switching to existence-lock semantics.

        .. versionadded:: 3.30.0

        """
        return self._settings.fallback_to_soft

    @property
    def preserve_lock_file(self) -> bool:
        """
        Whether filelock promises not to unlink the lock pathname on release.

        When ``True``, Windows skips its post-release unlink and Unix refuses the ``ENOSYS`` soft fallback.
        :class:`SoftFileLock` rejects ``True`` because unlinking its marker is how it releases.

        .. versionadded:: 3.30.0

        """
        return self._settings.preserve_lock_file

    @property
    def on_acquired(self) -> Callable[[int], None] | None:
        """
        The callback run with the borrowed lock descriptor once per physical acquisition, or ``None``.

        Native locks only. It runs after the native lock is held and backend initialization finished, before
        :meth:`~BaseFileLock.acquire` returns; a raise rolls back the acquisition. :class:`SoftFileLock` rejects it.

        .. versionadded:: 3.30.0

        """
        return self._settings.on_acquired

    @property
    def timeout(self) -> float:
        """
        The default timeout value, in seconds.

        .. versionadded:: 2.0.0

        """
        return self._context.timeout

    @timeout.setter
    def timeout(self, value: float | str | bytes | bytearray) -> None:
        if isinstance(value, (str, bytes, bytearray)):
            warnings.warn(
                "setting timeout from a string is deprecated and will be removed; pass a number",
                DeprecationWarning,
                stacklevel=2,
            )
            value = float(value)
        self._context.timeout = seconds("timeout", value)

    @property
    def blocking(self) -> bool:
        """
        Whether the locking is blocking or not.

        .. versionadded:: 3.14.0

        """
        return self._context.blocking

    @blocking.setter
    def blocking(self, value: bool) -> None:
        check_blocking(value)
        self._context.blocking = value

    @property
    def poll_interval(self) -> float:
        """
        The default polling interval, in seconds.

        .. versionadded:: 3.24.0

        """
        return self._context.poll_interval

    @poll_interval.setter
    def poll_interval(self, value: float) -> None:
        self._context.poll_interval = resolve_poll_interval(value)

    @property
    def lifetime(self) -> float | None:
        """
        The soft marker age in seconds that permits expiry, or ``None`` to disable age-based expiry.

        A non-``None`` value permits a waiter to enter while the previous holder remains active, so it does not provide
        strict mutual exclusion. Native locks ignore the value with a warning.

        .. versionadded:: 3.24.0

        """
        return self._context.lifetime

    @lifetime.setter
    def lifetime(self, value: float | None) -> None:
        self._context.lifetime = resolve_lifetime(value, type(self), stacklevel=3)

    @property
    def mode(self) -> int:
        """
        The file permissions requested for the lock file.

        Without an explicit ``mode`` this reports ``0o644`` for backwards compatibility, but the file is created with
        ``0o666`` filtered through the umask and default ACLs, so with a ``002`` umask it ends up ``0o664``. Check
        :attr:`has_explicit_mode` before relying on the value, or ``os.stat`` the file for its actual permissions.

        """
        return 0o644 if self._context.mode == UNSET_FILE_MODE else self._context.mode

    @property
    def has_explicit_mode(self) -> bool:
        """Whether the file permissions were explicitly set."""
        return self._context.mode != UNSET_FILE_MODE

    @property
    def lock_counter(self) -> int:
        """The number of times this lock has been acquired (but not yet released)."""
        ensure_current_process()
        return self._context.lock_counter

    def _set_claim_deadline(self, start_time: float, timeout: float) -> None:
        self._context.claim_deadline = None if timeout < 0 else start_time + timeout

    def _register_unverified_context_descriptor(self) -> None:
        if (
            self._context.lock_file_fd is not None and self._context.lock_file_fd_token is None
        ):  # pragma: no branch  # only an untracked descriptor needs registration
            if not HAS_REGISTER_AT_FORK:  # pragma: lacks fork
                return
            with fork_transition():  # pragma: needs fork
                self._context.lock_file_fd_token = record_owned_descriptor(self._context.lock_file_fd, None)

    def _unregister_released_descriptor(self) -> None:
        if self._context.lock_file_fd is None:
            if (token := self._context.lock_file_fd_token) is not None:  # pragma: needs fork
                unregister_owned_descriptor(token)
            self._context.lock_file_fd_token = None
            self._context.lock_file_fd_identity = None

    def _register_context_descriptor(self) -> None:
        if self._context.lock_file_fd is not None and self._context.lock_file_fd_token is None:
            self._context.lock_file_fd_token = register_owned_descriptor(
                self._context.lock_file_fd,
                self._context.lock_file_fd_identity,
            )

    def is_thread_local(self) -> bool:
        """:returns: a flag indicating if this lock is thread local or not"""
        return self._settings.thread_local

    def _close_released_fd(self, fd: int, *, default_suppresses: bool) -> None:
        # Retrying close after EINTR could close a reused descriptor.
        try:
            os.close(fd)
        except OSError:
            if not self._suppresses_close_error(default_suppresses=default_suppresses):
                raise

    def _suppresses_close_error(self, *, default_suppresses: bool) -> bool:
        return self._settings.close_error_policy == "suppress" or (
            self._settings.close_error_policy == "default" and default_suppresses
        )

    def _open_mode(self) -> int:
        # The filesystem applies umask and default ACLs when no mode was specified.
        return 0o666 if self._context.mode == UNSET_FILE_MODE else self._context.mode

    def _mark_descriptor_owned(self, fd: int, identity: tuple[int, int] | None = None) -> None:
        state: Final[FileLockContext] = self._hold_state()
        # Held first, pending cleared second: an interrupt between the two then leaves the descriptor recorded.
        state.lock_file_fd = fd
        state.unlock_uncertain = False
        state.lock_file_fd_identity = identity
        state.pending_lock_file_fd = None
        state.pending_lock_file_fd_identity = None
        if isinstance(self._context, ThreadLocalFileContext):
            self._thread_pending.pop(id(state), None)
            self._thread_holds[id(state)] = state, current_thread()

    def _hold_state(self) -> FileLockContext:
        return self._context.current if isinstance(self._context, ThreadLocalFileContext) else self._context

    def _mark_descriptor_pending(self, fd: int, identity: tuple[int, int] | None = None) -> None:
        state: Final[FileLockContext] = self._hold_state()
        state.pending_lock_file_fd = fd
        state.pending_lock_file_fd_identity = identity
        if isinstance(self._context, ThreadLocalFileContext):
            self._thread_pending[id(state)] = state, current_thread()

    def _mark_descriptor_released(self) -> None:
        state: Final[FileLockContext] = self._hold_state()
        state.pending_lock_file_fd = None
        state.pending_lock_file_fd_identity = None
        state.lock_file_fd = None
        state.unlock_uncertain = False
        state.lock_file_fd_identity = None
        if isinstance(self._context, ThreadLocalFileContext):
            self._thread_pending.pop(id(state), None)
            if state.strict_release is not None:  # pragma: win32 cover
                return
            self._thread_holds.pop(id(state), None)

    @override
    def _reset_after_fork_in_child(self) -> None:  # pragma: forked child
        # Discard parent state even when a PID namespace gives the child the same numeric PID.
        self._transition_lock = RLock()
        self._creator_pid = -1
        if isinstance(self._context, ThreadLocalFileContext):
            self._thread_holds.clear()
            self._thread_pending.clear()
        self._context.soft_fallback = False
        self._flockless_roots = frozenset()
        self._context.owner_claim_paths = ()
        self._context.claim_protection = None
        self._context.claim_root = None
        self._context.lock_file_fd = None
        self._context.unlock_uncertain = False
        self._context.lock_file_fd_token = None
        self._context.lock_file_fd_identity = None
        self._context.pending_lock_file_fd = None
        self._context.pending_lock_file_fd_identity = None
        self._context.lock_counter = 0
        self._context.releasing = None
        # The forking thread's own acquire continues in the child and removes its record when it ends.
        self._context.in_flight = [each for each in self._context.in_flight if on_this_stack(each.frame)]
        self._context.acquiring = self._context.in_flight[-1].frame if self._context.in_flight else None
        self._context.lock_file_key = None
        self._context.lock_file_registry = None
        self._context.lease_claim = None

    @override
    def _descriptors_for_fork(self) -> tuple[tuple[int, tuple[int, int] | None], ...]:  # pragma: needs fork
        descriptors: list[tuple[int, tuple[int, int] | None]] = []
        if self._context.lock_file_fd is not None and self._context.lock_file_fd_token is None:
            descriptors.append((self._context.lock_file_fd, self._context.lock_file_fd_identity))
        if self._context.pending_lock_file_fd is not None:
            descriptors.append((self._context.pending_lock_file_fd, self._context.pending_lock_file_fd_identity))
        return tuple(descriptors)


__all__ = [
    "LockState",
]
