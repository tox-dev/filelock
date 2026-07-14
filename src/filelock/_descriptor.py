"""A minimal native lock over a caller-owned file descriptor, contending with :class:`FileLock` on the same file."""

from __future__ import annotations

import sys
import time

if sys.platform == "win32":  # pragma: win32 cover
    from ._windows import _lock_fd_nonblocking, _unlock_fd
else:  # pragma: win32 no cover
    from ._unix import _lock_fd_nonblocking, _unlock_fd


def lock_descriptor(fd: int, *, blocking: bool = True, poll_interval: float = 0.05) -> bool:
    """
    Take the native OS lock on *fd*, a file descriptor the caller opened and owns.

    This is the same one-byte exclusive lock :class:`FileLock` uses, so a descriptor lock and a path lock on the same
    file contend with each other. Unlike :class:`FileLock` it adds no path handling: it never opens, truncates, closes,
    unlinks, chmods, canonicalizes, or falls back. The caller owns *fd* before, during, and after the call, and must
    close it. On Windows *fd* must be a synchronous descriptor (its handle not opened with ``FILE_FLAG_OVERLAPPED``).

    For timeout, reentrancy, singleton, lifetime, or stale-break behavior, use :class:`FileLock`. There is no async
    wrapper: run this in an executor, or drive ``blocking=False`` from your own polling loop.

    :param fd: an open file descriptor the caller owns.
    :param blocking: when ``True`` (default), retry the nonblocking attempt every *poll_interval* seconds until it
        succeeds; when ``False``, make one attempt.
    :param poll_interval: seconds between attempts while blocking.

    :returns: ``True`` once the lock is held, or ``False`` on contention when ``blocking`` is ``False``.

    :raises OSError: for a permanent native failure, such as an invalid descriptor. The descriptor is left open.

    .. versionadded:: 3.30.0

    """
    if not blocking:
        return _lock_fd_nonblocking(fd)
    while not _lock_fd_nonblocking(fd):
        time.sleep(poll_interval)
    return True


def unlock_descriptor(fd: int) -> None:
    """
    Release the native OS lock on *fd* without touching the descriptor.

    :param fd: the descriptor a prior :func:`lock_descriptor` locked; the caller still owns and must close it.

    :raises OSError: if the native unlock fails; the caller may retry on the same descriptor.

    .. versionadded:: 3.30.0

    """
    _unlock_fd(fd)


__all__ = [
    "lock_descriptor",
    "unlock_descriptor",
]
