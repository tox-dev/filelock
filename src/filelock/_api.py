from __future__ import annotations

import contextlib
import inspect
import logging
import os
import sys
import time
import warnings
from abc import ABCMeta, abstractmethod
from dataclasses import dataclass
from threading import Lock, local
from typing import TYPE_CHECKING, Any, Final, Literal, NoReturn, TypeVar, cast
from weakref import WeakValueDictionary

from ._error import Timeout
from ._util import break_lock_file

#: No explicit file permission mode was passed. Lock files then open with 0o666 so umask and default ACLs pick
#: the final permissions, and fchmod is skipped to preserve POSIX default ACL inheritance.
_UNSET_FILE_MODE: Final[int] = -1

#: How a context manager reconciles a body failure with a release failure on exit (see the property of this name).
ContextErrorPolicy = Literal["chain", "group"]
_CONTEXT_ERROR_POLICIES: Final[frozenset[str]] = frozenset({"chain", "group"})

#: What a native backend does with an ``os.close`` failure after the OS unlock committed (see the property).
CloseErrorPolicy = Literal["default", "raise", "suppress"]
_CLOSE_ERROR_POLICIES: Final[frozenset[str]] = frozenset({"default", "raise", "suppress"})

if TYPE_CHECKING:
    from collections.abc import Callable
    from types import TracebackType

    from ._read_write import ReadWriteLock
    from ._soft_rw import SoftReadWriteLock

    if sys.version_info >= (3, 11):  # pragma: no cover (py311+)
        from typing import Self
    else:  # pragma: no cover (<py311)
        from typing_extensions import Self


_LOGGER: Final[logging.Logger] = logging.getLogger("filelock")


def _exception_group_cls() -> type[BaseException]:
    # BaseExceptionGroup is a builtin on 3.11+; on 3.10 it needs the exceptiongroup backport. filelock keeps zero
    # runtime dependencies, so the backport is imported lazily rather than required, and only group mode needs it.
    if sys.version_info >= (3, 11):  # pragma: no cover (py311+)
        return BaseExceptionGroup  # noqa: F821  # builtin on 3.11+
    # Alias the import so BaseExceptionGroup above stays the builtin rather than an unbound local of this function.
    from exceptiongroup import BaseExceptionGroup as _Backport  # noqa: PLC0415  # pragma: no cover (<py311)

    return _Backport  # pragma: no cover (<py311)


def _resolve_context_error_policy(policy: str) -> ContextErrorPolicy:
    if policy not in _CONTEXT_ERROR_POLICIES:
        msg = f"context_error_policy must be 'chain' or 'group', got {policy!r}"
        raise ValueError(msg)
    if policy == "group":  # fail fast at construction rather than only when a dual failure happens to occur
        try:
            _exception_group_cls()
        except ImportError as exc:  # pragma: no cover  # only on 3.10 without the exceptiongroup backport
            msg = "context_error_policy='group' requires Python 3.11+ or the 'exceptiongroup' backport installed"
            raise ValueError(msg) from exc
    return cast("ContextErrorPolicy", policy)


def _resolve_close_error_policy(policy: str) -> CloseErrorPolicy:
    if policy not in _CLOSE_ERROR_POLICIES:
        msg = f"close_error_policy must be 'default', 'raise', or 'suppress', got {policy!r}"
        raise ValueError(msg)
    return cast("CloseErrorPolicy", policy)


def _raise_body_and_release(body_error: BaseException, release_error: BaseException) -> NoReturn:
    # Group mode: surface the body failure and the release failure as sibling leaves instead of letting one hide in the
    # other's __context__. BaseExceptionGroup returns a plain ExceptionGroup when both leaves subclass Exception, so
    # ``except*`` and ``except Exception`` still catch them; a BaseException leaf (KeyboardInterrupt, CancelledError)
    # keeps the group outside ordinary handlers. ``from None`` stops the group itself gaining a redundant __context__.
    msg = "lock body and release both failed"
    raise _exception_group_cls()(msg, (body_error, release_error)) from None


# On Windows os.path.realpath calls CreateFileW with share_mode=0, which blocks concurrent DeleteFileW and causes
# livelocks under threaded contention with SoftFileLock. os.path.abspath is purely string-based and avoids this.
_resolve_dir: Final[Callable[[str], str]] = os.path.abspath if sys.platform == "win32" else os.path.realpath


def _canonical(path: str | os.PathLike[str]) -> str:
    """
    Return one stable key for *path*, collapsing equivalent spellings without following a final symlink.

    Relative, absolute, and ``./`` spellings of one lock file must map to a single singleton instance, deadlock-registry
    entry, and removal key. Resolving the whole path with ``realpath`` would follow a final symlink and alias a lock
    target the backend deliberately rejects, so the registry identity would differ from the backend's. Resolving only
    the parent directory and re-appending the literal final component collapses the equivalent spellings while keeping a
    final symlink a distinct key. On Windows the parent is resolved with ``abspath`` so junctions and reparse points are
    not followed either.
    """
    parent, name = os.path.split(os.fspath(path))
    return os.path.join(_resolve_dir(parent or os.curdir), name)  # noqa: PTH118  # string join matches abspath/realpath


def _resolve_lifetime(lifetime: float | None, *, supported: bool, cls_name: str) -> float | None:
    """
    Drop a ``lifetime`` a lock cannot honor.

    ``lifetime`` is a deliberate age-based lease: a lock file older than ``lifetime`` is broken even while its holder is
    still alive. That is only safe for existence locks (:class:`SoftFileLock`), where breaking means unlinking a
    pathname the protocol already treats as reclaimable. A native OS lock lives on the inode, so unlinking the pathname
    by age cannot revoke the kernel lock; a contender would lock a fresh inode and overlap the live holder (#590).
    Ignore the request with a warning rather than accept a setting that breaks mutual exclusion.
    """
    if lifetime is not None and not supported:
        warnings.warn(
            f"lifetime is ignored for {cls_name}: a native OS lock cannot be broken safely by file age; "
            f"only SoftFileLock supports lifetime-based expiry",
            stacklevel=3,
        )
        return None
    return lifetime


class _ThreadLocalRegistry(local):
    def __init__(self) -> None:
        super().__init__()
        self.held: dict[str, int] = {}


_registry: Final[_ThreadLocalRegistry] = _ThreadLocalRegistry()


_T = TypeVar("_T", bound="BaseFileLock")


class FileLockMeta(ABCMeta):
    _instances: WeakValueDictionary[str, BaseFileLock]
    _instances_lock: Lock

    def __call__(  # noqa: PLR0913
        cls: type[_T],
        lock_file: str | os.PathLike[str],
        timeout: float = -1,
        mode: int = _UNSET_FILE_MODE,
        thread_local: bool = True,  # noqa: FBT001, FBT002
        *,
        blocking: bool = True,
        is_singleton: bool = False,
        poll_interval: float = 0.05,
        lifetime: float | None = None,
        context_error_policy: ContextErrorPolicy = "chain",
        close_error_policy: CloseErrorPolicy = "default",
        fallback_to_soft: bool = True,
        **kwargs: Any,  # capture remaining kwargs for subclasses  # noqa: ANN401
    ) -> _T:
        lifetime = _resolve_lifetime(lifetime, supported=cls._lifetime_supported, cls_name=cls.__name__)
        # Validate before building the instance: a raise inside __init__ would leave a half-constructed object whose
        # __del__ then trips over the missing context.
        context_error_policy = _resolve_context_error_policy(context_error_policy)
        close_error_policy = _resolve_close_error_policy(close_error_policy)
        params = {
            "timeout": timeout,
            "mode": mode,
            "thread_local": thread_local,
            "blocking": blocking,
            "is_singleton": is_singleton,
            "poll_interval": poll_interval,
            "lifetime": lifetime,
            "context_error_policy": context_error_policy,
            "close_error_policy": close_error_policy,
            "fallback_to_soft": fallback_to_soft,
            **kwargs,
        }
        if not is_singleton:
            return cls._create_instance(lock_file, params)

        # Look up, build and store under one lock. Without it two threads racing the first construction for a
        # path both miss the cache and each build their own instance, so callers relying on is_singleton for
        # reentrant locking across instances end up with two "singletons" and acquire()'s deadlock check then
        # rejects a legitimate reentrant acquire; the unguarded writes to the WeakValueDictionary are a data
        # race besides. ReadWriteLock and SoftReadWriteLock already guard their singleton caches this way.
        # Key the cache on the canonical form so equivalent spellings of one path share a singleton, and it matches the
        # deadlock-registry key acquire() uses.
        singleton_key = _canonical(lock_file)
        with cls._instances_lock:
            if (instance := cls._instances.get(singleton_key)) is None:
                instance = cls._create_instance(lock_file, params)
                cls._instances[singleton_key] = instance
                return instance

        params_to_check = {
            "thread_local": (thread_local, instance.is_thread_local()),
            "timeout": (timeout, instance.timeout),
            "mode": (mode, instance._context.mode),  # noqa: SLF001
            "blocking": (blocking, instance.blocking),
            "poll_interval": (poll_interval, instance.poll_interval),
            "lifetime": (lifetime, instance.lifetime),
            "context_error_policy": (context_error_policy, instance.context_error_policy),
            "close_error_policy": (close_error_policy, instance.close_error_policy),
            "fallback_to_soft": (fallback_to_soft, instance.fallback_to_soft),
        }
        non_matching_params = {
            name: (passed_param, set_param)
            for name, (passed_param, set_param) in params_to_check.items()
            if passed_param != set_param
        }
        if not non_matching_params:
            return instance  # ty: ignore[invalid-return-type]  # https://github.com/astral-sh/ty/issues/3231

        msg = "Singleton lock instances cannot be initialized with differing arguments"
        msg += "\nNon-matching arguments: "
        for param_name, (passed_param, set_param) in non_matching_params.items():
            msg += f"\n\t{param_name} (existing lock has {set_param} but {passed_param} was passed)"
        raise ValueError(msg)

    def _create_instance(cls: type[_T], lock_file: str | os.PathLike[str], params: dict[str, Any]) -> _T:
        # Keep only the params this subclass's __init__ accepts. virtualenv narrows its BaseFileLock
        # descendant's signature, so passing the full set breaks it (tox-dev/filelock#340).
        present_params = inspect.signature(cls.__init__).parameters
        return super().__call__(lock_file, **{key: value for key, value in params.items() if key in present_params})


class BaseFileLock(contextlib.ContextDecorator, metaclass=FileLockMeta):  # noqa: PLR0904  # public config properties
    """
    Abstract base class for a file lock object.

    Provides a reentrant, cross-process exclusive lock backed by OS-level primitives. Subclasses implement the actual
    locking mechanism (:class:`UnixFileLock <filelock.UnixFileLock>`, :class:`WindowsFileLock
    <filelock.WindowsFileLock>`, :class:`SoftFileLock <filelock.SoftFileLock>`).

    """

    _instances: WeakValueDictionary[str, BaseFileLock]
    _instances_lock: Lock

    #: How the cross-instance deadlock message names the conflicting holder; the async subclass says "task".
    _deadlock_holder_desc: str = "FileLock instance in this thread"

    #: Whether an age-based :attr:`lifetime` lease may break this lock. Only existence locks set it (they reclaim by
    #: unlinking a pathname); native OS locks leave it ``False`` since a kernel lock cannot be revoked by file age.
    _lifetime_supported: bool = False

    def __init_subclass__(cls, **kwargs: dict[str, Any]) -> None:
        """Give each lock subclass its own singleton registry and lock."""
        super().__init_subclass__(**kwargs)
        cls._instances = WeakValueDictionary()
        cls._instances_lock = Lock()

    def __init__(  # noqa: PLR0913
        self,
        lock_file: str | os.PathLike[str],
        timeout: float = -1,
        mode: int = _UNSET_FILE_MODE,
        thread_local: bool = True,  # noqa: FBT001, FBT002
        *,
        blocking: bool = True,
        is_singleton: bool = False,
        poll_interval: float = 0.05,
        lifetime: float | None = None,
        context_error_policy: ContextErrorPolicy = "chain",
        close_error_policy: CloseErrorPolicy = "default",
        fallback_to_soft: bool = True,
    ) -> None:
        """
        Create a new lock object.

        :param lock_file: path to the file
        :param timeout: default timeout when acquiring the lock, in seconds. It will be used as fallback value in the
            acquire method, if no timeout value (``None``) is given. If you want to disable the timeout, set it to a
            negative value. A timeout of 0 means that there is exactly one attempt to acquire the file lock.
        :param mode: file permissions for the lockfile. When not specified, the OS controls permissions via umask and
            default ACLs, preserving POSIX default ACL inheritance in shared directories.
        :param thread_local: Whether this object's internal context should be thread local or not. If this is set to
            ``False`` then the lock will be reentrant across threads. When ``True`` (the default), **all fields of the
            lock's internal context are per-thread**, including the configuration values ``poll_interval``, ``timeout``,
            ``blocking``, ``mode``, and ``lifetime``. Setting one of these properties from one thread does not change
            the value seen by another thread; threads that did not perform the write continue to see the value supplied
            at construction time. If you need configuration values to be visible across threads, construct the lock
            with ``thread_local=False``.
        :param blocking: whether the lock should be blocking or not
        :param is_singleton: If this is set to ``True`` then only one instance of this class will be created per lock
            file. This is useful if you want to use the lock object for reentrant locking without needing to pass the
            same object around.
        :param poll_interval: default interval for polling the lock file, in seconds. It will be used as fallback value
            in the acquire method, if no poll_interval value (``None``) is given.
        :param lifetime: for :class:`SoftFileLock`, the maximum time in seconds a lock may be held before it expires: a
            waiting process breaks a lock file whose modification time is older than ``lifetime`` seconds, even if the
            holder is still alive. ``None`` (the default) means locks never expire. Native OS locks (:class:`FileLock`)
            cannot be revoked by file age and ignore a non-``None`` ``lifetime`` with a warning.
        :param context_error_policy: how a context manager reconciles a failure in its body with a failure while
            releasing on exit. ``"chain"`` (the default) keeps Python's behavior: the release error propagates with the
            body error in its ``__context__``. ``"group"`` raises a :class:`BaseExceptionGroup` holding the body error
            first and the release error second, so neither hides the other.
        :param close_error_policy: for native locks (:class:`FileLock`), what to do with an ``os.close`` failure after
            the OS unlock has already committed. ``"default"`` keeps each platform's historical behavior (Unix drops a
            FUSE/Docker ``EIO``, Windows propagates); ``"raise"`` always propagates the ``OSError``; ``"suppress"``
            always ignores it. Held state is released either way. It does not affect unlock failures or lock-file
            deletion.
        :param fallback_to_soft: for :class:`UnixFileLock`, whether to switch to :class:`SoftFileLock` when the
            filesystem's ``flock`` returns ``ENOSYS``. ``True`` (the default) keeps the historical fallback;
            ``False`` fails closed, letting the ``ENOSYS`` propagate so a caller that needs kernel-enforced
            locking is never silently downgraded. It has no effect on Windows or :class:`SoftFileLock`.

        """
        self._is_thread_local = thread_local
        self._is_singleton = is_singleton
        self._context_error_policy = context_error_policy  # already validated by the metaclass
        self._close_error_policy = close_error_policy  # already validated by the metaclass
        self._fallback_to_soft = fallback_to_soft

        # External code reaches these values through the public properties, not through _context directly.
        kwargs: dict[str, Any] = {
            "lock_file": os.fspath(lock_file),
            "timeout": timeout,
            "mode": mode,
            "blocking": blocking,
            "poll_interval": poll_interval,
            "lifetime": lifetime,
        }
        self._context: FileLockContext = (ThreadLocalFileContext if thread_local else FileLockContext)(**kwargs)

    def is_thread_local(self) -> bool:
        """:returns: a flag indicating if this lock is thread local or not"""
        return self._is_thread_local

    @property
    def is_singleton(self) -> bool:
        """
        A flag indicating if this lock is singleton or not.

        .. versionadded:: 3.13.0

        """
        return self._is_singleton

    @property
    def context_error_policy(self) -> ContextErrorPolicy:
        """
        How a context manager reconciles a body failure with a release failure on exit.

        .. versionadded:: 3.27.0

        """
        return self._context_error_policy

    @property
    def close_error_policy(self) -> CloseErrorPolicy:
        """
        What a native lock does with an ``os.close`` failure after the OS unlock committed.

        .. versionadded:: 3.27.0

        """
        return self._close_error_policy

    def _close_released_fd(self, fd: int, *, default_suppresses: bool) -> None:
        # Close the descriptor after the OS unlock has committed. CPython never retries close() after EINTR because the
        # descriptor number may already be reused, so neither does this. close_error_policy decides the error's fate:
        # "raise" propagates, "suppress" drops it, "default" keeps the backend's historical behavior (Unix suppresses a
        # FUSE/Docker EIO, Windows propagates). Held state is already cleared, so the lock stays released either way.
        try:
            os.close(fd)
        except OSError:
            if self._close_error_policy == "suppress" or (self._close_error_policy == "default" and default_suppresses):
                return
            raise

    @property
    def fallback_to_soft(self) -> bool:
        """
        Whether a :class:`FileLock` falls back to :class:`SoftFileLock` when the filesystem lacks ``flock``.

        Only :class:`UnixFileLock` acts on it: when ``False`` an ``ENOSYS`` from ``flock`` propagates instead of
        switching to existence-lock semantics.

        .. versionadded:: 3.27.0

        """
        return self._fallback_to_soft

    @property
    def lock_file(self) -> str:
        """Path to the lock file."""
        return self._context.lock_file

    @property
    def timeout(self) -> float:
        """
        The default timeout value, in seconds.

        .. versionadded:: 2.0.0

        """
        return self._context.timeout

    @timeout.setter
    def timeout(self, value: float | str) -> None:
        """
        Change the default timeout value.

        :param value: the new value, in seconds

        """
        self._context.timeout = float(value)

    @property
    def blocking(self) -> bool:
        """
        Whether the locking is blocking or not.

        .. versionadded:: 3.14.0

        """
        return self._context.blocking

    @blocking.setter
    def blocking(self, value: bool) -> None:
        """
        Change the default blocking value.

        :param value: the new value as bool

        """
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
        """
        Change the default polling interval.

        :param value: the new value, in seconds

        """
        self._context.poll_interval = value

    @property
    def lifetime(self) -> float | None:
        """
        The lock lifetime in seconds, or ``None`` if the lock never expires.

        .. versionadded:: 3.24.0

        """
        return self._context.lifetime

    @lifetime.setter
    def lifetime(self, value: float | None) -> None:
        """
        Change the lock lifetime.

        :param value: the new value in seconds, or ``None`` to disable expiration

        :raises ValueError: if *value* is a negative number
        :raises TypeError: if *value* is not ``None`` and not a real number

        """
        if value is not None:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                msg = f"lifetime must be a non-negative number or None, not {type(value).__name__}"
                raise TypeError(msg)
            if value < 0:
                msg = f"lifetime must be non-negative, not {value!r}"
                raise ValueError(msg)
        self._context.lifetime = _resolve_lifetime(
            value, supported=self._lifetime_supported, cls_name=type(self).__name__
        )

    @property
    def mode(self) -> int:
        """The file permissions for the lockfile."""
        return 0o644 if self._context.mode == _UNSET_FILE_MODE else self._context.mode

    @property
    def has_explicit_mode(self) -> bool:
        """Whether the file permissions were explicitly set."""
        return self._context.mode != _UNSET_FILE_MODE

    def _open_mode(self) -> int:
        """Mode for ``os.open``: 0o666 when unset so umask and ACLs decide, otherwise the explicit mode."""
        return 0o666 if self._context.mode == _UNSET_FILE_MODE else self._context.mode

    @property
    def is_locked(self) -> bool:
        """
        A boolean indicating if the lock file is holding the lock currently.

        .. versionchanged:: 2.0.0

            This was previously a method and is now a property.

        """
        return self._context.lock_file_fd is not None

    @property
    def lock_counter(self) -> int:
        """The number of times this lock has been acquired (but not yet released)."""
        return self._context.lock_counter

    def __enter__(self) -> Self:
        """
        Acquire the lock.

        :returns: the lock object

        """
        self.acquire()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Release the lock, reconciling a release failure with any body failure per :attr:`context_error_policy`."""
        self._release_in_context(exc_value)

    def _release_in_context(self, body_error: BaseException | None) -> None:
        # Release from a context-manager exit. "chain" lets a release failure propagate with the body error already in
        # its __context__ (Python's default); "group" raises both as sibling leaves so neither one hides the other.
        try:
            self.release()
        except BaseException as release_error:
            if body_error is None or self._context_error_policy == "chain":
                raise
            _raise_body_and_release(body_error, release_error)

    def __del__(self) -> None:
        """Force-release so a dropped reference never leaks a held lock."""
        self.release(force=True)

    def acquire(
        self,
        timeout: float | None = None,
        poll_interval: float | None = None,
        *,
        poll_intervall: float | None = None,
        blocking: bool | None = None,
        cancel_check: Callable[[], bool] | None = None,
    ) -> AcquireReturnProxy:
        """
        Try to acquire the file lock.

        :param timeout: maximum wait time for acquiring the lock, ``None`` means use the default :attr:`~timeout` is and
            if ``timeout < 0``, there is no timeout and this method will block until the lock could be acquired
        :param poll_interval: interval of trying to acquire the lock file, ``None`` means use the default
            :attr:`~poll_interval`
        :param poll_intervall: deprecated, kept for backwards compatibility, use ``poll_interval`` instead
        :param blocking: defaults to True. If False, function will return immediately if it cannot obtain a lock on the
            first attempt. Otherwise, this method will block until the timeout expires or the lock is acquired.
        :param cancel_check: a callable returning ``True`` when the acquisition should be canceled. Checked on each poll
            iteration. When triggered, raises :class:`~Timeout` just like an expired timeout.

        :returns: a context object that will unlock the file when the context is exited

        :raises Timeout: if fails to acquire lock within the timeout period

        .. code-block:: python

            # You can use this method in the context manager (recommended)
            with lock.acquire():
                pass

            # Or use an equivalent try-finally construct:
            lock.acquire()
            try:
                pass
            finally:
                lock.release()

        .. versionchanged:: 2.0.0

            This method returns now a *proxy* object instead of *self*, so that it can be used in a with statement
            without side effects.

        """
        if timeout is None:
            timeout = self._context.timeout

        if blocking is None:
            blocking = self._context.blocking

        if poll_intervall is not None:
            msg = "use poll_interval instead of poll_intervall"
            warnings.warn(msg, DeprecationWarning, stacklevel=2)
            poll_interval = poll_intervall

        poll_interval = poll_interval if poll_interval is not None else self._context.poll_interval

        # Bump the counter up front; _undo_acquire rolls it back if acquisition fails.
        self._context.lock_counter += 1

        canonical = _canonical(self.lock_file)
        self._raise_if_would_deadlock(canonical, timeout=timeout, blocking=blocking)

        start_time = time.perf_counter()
        try:
            self._poll_until_acquired(
                blocking=blocking,
                cancel_check=cancel_check,
                timeout=timeout,
                poll_interval=poll_interval,
                start_time=start_time,
            )
        except BaseException:
            self._undo_acquire(canonical)
            raise
        self._commit_acquire(canonical)
        return AcquireReturnProxy(lock=self)

    def release(self, force: bool = False) -> None:  # noqa: FBT001, FBT002
        """
        Release the file lock. The lock is only completely released when the lock counter reaches 0. The lock file
        itself may be deleted automatically, the behavior is platform-specific.

        :param force: If true, the lock counter is ignored and the lock is released in every case.

        """
        if not self.is_locked:
            return
        if not force and self._context.lock_counter > 1:
            self._context.lock_counter -= 1
            return

        lock_id, lock_filename = id(self), self.lock_file
        _LOGGER.debug("Attempting to release lock %s on %s", lock_id, lock_filename)
        try:
            self._release()
        except BaseException:
            # A failure after the OS unlock (during close or unlink) still released the lock: the backend cleared
            # its descriptor, so commit the counter and registry to released even as the cleanup error propagates.
            # A failure that left the lock held keeps the counter so a later release can retry the OS unlock.
            if not self.is_locked:
                self._commit_release()
            raise
        self._commit_release()
        _LOGGER.debug("Lock %s released on %s", lock_id, lock_filename)

    def _raise_if_would_deadlock(self, canonical: str, *, timeout: float, blocking: bool) -> None:
        """
        Fail fast when a *different* live instance already holds this path on the current thread/task.

        Only the first, indefinitely-blocking acquire can self-deadlock this way: waiting in the OS primitive would
        block on a lock this thread already owns. A finite timeout or ``blocking=False`` keeps the normal Timeout path.
        """
        would_block = self._context.lock_counter == 1 and not self.is_locked and timeout < 0 and blocking
        if would_block and _registry.held.get(canonical) not in {None, id(self)}:
            self._context.lock_counter -= 1
            msg = (
                f"Deadlock: lock '{self.lock_file}' is already held by a different {self._deadlock_holder_desc}. "
                f"Use is_singleton=True to enable reentrant locking across instances."
            )
            raise RuntimeError(msg)

    def _poll_until_acquired(
        self,
        *,
        blocking: bool,
        cancel_check: Callable[[], bool] | None,
        timeout: float,
        poll_interval: float,
        start_time: float,
    ) -> None:
        lock_id = id(self)
        lock_filename = self.lock_file
        while True:
            if not self.is_locked:
                self._try_break_expired_lock()
                _LOGGER.debug("Attempting to acquire lock %s on %s", lock_id, lock_filename)
                self._acquire()
            if self.is_locked:
                _LOGGER.debug("Lock %s acquired on %s", lock_id, lock_filename)
                return
            if self._check_give_up(
                lock_id,
                lock_filename,
                blocking=blocking,
                cancel_check=cancel_check,
                timeout=timeout,
                start_time=start_time,
            ):
                raise Timeout(lock_filename)
            msg = "Lock %s not acquired on %s, waiting %s seconds ..."
            _LOGGER.debug(msg, lock_id, lock_filename, poll_interval)
            time.sleep(poll_interval)

    def _undo_acquire(self, canonical: str) -> None:
        """Roll back the counter after a failed acquire, dropping the registry entry once nothing holds the path."""
        self._context.lock_counter = max(0, self._context.lock_counter - 1)
        if self._context.lock_counter == 0:
            _registry.held.pop(canonical, None)

    def _commit_acquire(self, canonical: str) -> None:
        """Record this instance as the holder once the first acquire succeeds, so peers can detect the deadlock."""
        if self._context.lock_counter == 1:
            _registry.held[canonical] = id(self)

    def _drop_registry_entry(self) -> None:
        """Forget this path's holder on release so a later cross-instance acquire is not misread as a deadlock."""
        _registry.held.pop(_canonical(self.lock_file), None)

    def _commit_release(self) -> None:
        """Record the lock as fully released: reset the recursion counter and drop the deadlock-registry entry."""
        self._context.lock_counter = 0
        self._drop_registry_entry()

    def _try_break_expired_lock(self) -> None:
        """Remove the lock file if its modification time exceeds the configured :attr:`lifetime`."""
        if (lifetime := self._context.lifetime) is None:
            return
        with contextlib.suppress(OSError):
            # lstat, not stat: an attacker with write access to the lock directory can replace a held
            # lock file with a symlink pointing at an old file, making stat() report the target's stale
            # mtime so a waiter breaks a live lock and two processes hold it at once. lstat reads the
            # symlink's own mtime, matching the O_NOFOLLOW reads elsewhere.
            st = os.lstat(self.lock_file)
            if time.time() - st.st_mtime < lifetime:
                return
            break_lock_file(self.lock_file, st.st_mtime, st.st_ino)

    @staticmethod
    def _check_give_up(  # noqa: PLR0913
        lock_id: int,
        lock_filename: str,
        *,
        blocking: bool,
        cancel_check: Callable[[], bool] | None,
        timeout: float,
        start_time: float,
    ) -> bool:
        if blocking is False:
            _LOGGER.debug("Failed to immediately acquire lock %s on %s", lock_id, lock_filename)
            return True
        if cancel_check is not None and cancel_check():
            _LOGGER.debug("Cancellation requested for lock %s on %s", lock_id, lock_filename)
            return True
        if 0 <= timeout < time.perf_counter() - start_time:
            _LOGGER.debug("Timeout on acquiring lock %s on %s", lock_id, lock_filename)
            return True
        return False

    @abstractmethod
    def _acquire(self) -> None:
        """If the file lock could be acquired, self._context.lock_file_fd holds the file descriptor of the lock file."""
        raise NotImplementedError

    @abstractmethod
    def _release(self) -> None:
        """Releases the lock and sets self._context.lock_file_fd to None."""
        raise NotImplementedError


# acquire() returns this wrapper instead of self so entering the with-statement does not call __enter__ a second
# time; returning self would re-acquire the lock in BaseFileLock.__enter__ without a matching release (issue #37).
class AcquireReturnProxy:
    """A context-aware object that will release the lock file when exiting."""

    def __init__(self, lock: BaseFileLock | ReadWriteLock | SoftReadWriteLock) -> None:
        self.lock: BaseFileLock | ReadWriteLock | SoftReadWriteLock = lock

    def __enter__(self) -> BaseFileLock | ReadWriteLock | SoftReadWriteLock:
        return self.lock

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if isinstance(self.lock, BaseFileLock):
            self.lock._release_in_context(exc_value)  # noqa: SLF001
        else:  # a reader/writer lock does not carry a context_error_policy
            self.lock.release()


@dataclass
class FileLockContext:
    """Holds the context for a ``BaseFileLock`` object."""

    # A separate class so ThreadLocalFileContext can make the whole context thread-local.

    lock_file: str
    timeout: float
    mode: int
    blocking: bool
    poll_interval: float

    #: The lock lifetime in seconds; ``None`` means the lock never expires.
    lifetime: float | None = None

    #: File descriptor from os.open for the lock file; not None while the lock is held.
    lock_file_fd: int | None = None

    #: Depth of nested acquisitions; the lock is released only when it returns to 0.
    lock_counter: int = 0


class ThreadLocalFileContext(FileLockContext, local):
    """A thread local version of the ``FileLockContext`` class."""


__all__ = [
    "_UNSET_FILE_MODE",
    "AcquireReturnProxy",
    "BaseFileLock",
]
