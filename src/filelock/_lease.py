from __future__ import annotations

import os
import secrets
import time
from contextlib import suppress
from dataclasses import dataclass
from threading import Event, Thread, current_thread
from typing import TYPE_CHECKING, Literal

from ._error import LeaseSettingsMismatch
from ._identity import owner_is_stale
from ._marker import MarkerSoftFileLock, OwnerMode, OwnerRecord, parse_marker
from ._soft import _read_lock_file
from ._util import break_lock_file, touch

if TYPE_CHECKING:
    import sys
    from collections.abc import Callable

    from ._api import LockOptions

    if sys.version_info >= (3, 11):  # pragma: no cover (py311+)
        from typing import Unpack
    else:  # pragma: no cover (<py311)
        from typing_extensions import Unpack

CompromiseReason = Literal["marker-missing", "owner-changed", "refresh-failed"]

_RefreshOutcome = Literal["ok", "lost", "transient"]


@dataclass(frozen=True)
class LeaseCompromise:
    """Why a held lease stopped being this process's to hold."""

    lock_file: str
    token: str
    reason: CompromiseReason
    error: OSError | None = None


class SoftFileLease(MarkerSoftFileLock):
    """
    Existence lock whose claim expires, so a peer may take it while the previous holder still runs.

    A lease trades mutual exclusion for progress. The holder publishes a claim and refreshes it every
    ``heartbeat_interval`` seconds; a contender takes the marker once it is ``lease_duration`` seconds stale. Nothing
    stops the expired holder: it keeps running, and it keeps using whatever the lock protects. Treat the lease as a hint
    about who *should* be working, not as a guarantee that only one worker is.

    To make a protected resource reject a superseded holder, that resource must be linearizable and must fence on a
    monotonic generation it controls. :attr:`token` names a claim; it does not fence one. Where overlap is unacceptable,
    use :class:`StrictSoftFileLock <filelock.StrictSoftFileLock>` instead.

    Every contender for a path must agree on ``lease_duration``. A contender that finds a claim published under a
    different duration raises :class:`LeaseSettingsMismatch <filelock.LeaseSettingsMismatch>` rather than apply its own
    expiry to a peer that never agreed to it.

    Expiry reclaims less on Windows, which refuses to rename or delete a file another process holds open. A peer there
    takes an expired claim only once the previous holder's process exits and its handle closes; a holder that lives on
    but stops refreshing keeps the marker. Unix reclaims the marker either way.

    ``on_compromise`` fires from the heartbeat thread when a refresh fails, or when the marker vanishes or names another
    owner. The holder should stop touching the protected resource when it runs. Because it runs on that thread, a
    ``release()`` inside it only takes effect when the lease was built with ``thread_local=False``; the default
    thread-local context hides the claim from every thread but the one that acquired it, so the release does nothing.
    Signal the acquiring thread instead when the context stays thread-local.

    .. versionadded:: 3.30.0

    """

    _owner_mode: OwnerMode = "lease"

    #: lease_duration replaces the legacy age-based lifetime, so accepting both would give one lock two expiry clocks.
    _lifetime_supported: bool = False
    _lifetime_unsupported_reason: str = "lease_duration sets when a lease expires"

    def __init__(
        self,
        lock_file: str | os.PathLike[str],
        *,
        lease_duration: float = 30.0,
        heartbeat_interval: float | None = None,
        on_compromise: Callable[[LeaseCompromise], None] | None = None,
        **kwargs: Unpack[LockOptions],
    ) -> None:
        """
        Create a lease.

        :param lease_duration: seconds of marker staleness after which a contender may take the claim. Every contender
            for the path must pass the same value.
        :param heartbeat_interval: seconds between refreshes. Defaults to a third of ``lease_duration``, leaving room
            for two missed refreshes before a peer may take the claim. Must be shorter than ``lease_duration``.
        :param on_compromise: called from the heartbeat thread with a :class:`LeaseCompromise` when the claim is lost.
        :param kwargs: every other :class:`BaseFileLock <filelock.BaseFileLock>` option, ``timeout`` and ``mode`` among
            them. The metaclass passes them all by keyword, and taking them here lets
            :class:`AsyncSoftFileLease <filelock.AsyncSoftFileLease>` add the async plumbing a fixed signature would
            hide.

        """
        if lease_duration <= 0:
            msg = f"lease_duration must be positive, got {lease_duration!r}"
            raise ValueError(msg)
        if heartbeat_interval is None:
            heartbeat_interval = lease_duration / 3
        if not 0 < heartbeat_interval < lease_duration:
            msg = f"heartbeat_interval must be positive and below lease_duration, got {heartbeat_interval!r}"
            raise ValueError(msg)
        super().__init__(lock_file, **kwargs)
        self._lease_duration = lease_duration
        self._heartbeat_interval = heartbeat_interval
        self._on_compromise = on_compromise
        self._token: str | None = None
        self._compromise: LeaseCompromise | None = None
        self._heartbeat_stop = Event()
        self._heartbeat: Thread | None = None

    @property
    def lease_duration(self) -> float:
        """The staleness in seconds after which a contender may take this claim."""
        return self._lease_duration

    @property
    def token(self) -> str | None:
        """
        The token naming the claim this process published.

        :returns: the token while the lease is held, ``None`` otherwise. It identifies a claim; it does not fence one.

        """
        return self._token

    @property
    def compromise(self) -> LeaseCompromise | None:
        """
        The loss of claim the heartbeat observed.

        :returns: the :class:`LeaseCompromise`, or ``None`` while the claim still holds

        """
        return self._compromise

    def _acquire(self) -> None:
        self._stop_heartbeat()  # no earlier claim's heartbeat outlives the acquisition of the next one
        self._token = secrets.token_hex(16)
        self._compromise = None
        super()._acquire()
        # The context is thread-local by default, so the heartbeat thread cannot read the descriptor this one just
        # published. Hand it the fd and the inode it verified instead.
        if (fd := self._context.lock_file_fd) is not None and (
            identity := self._context.lock_file_fd_identity
        ) is not None:
            self._start_heartbeat(fd, identity, self._token)

    def _release(self) -> None:
        self._stop_heartbeat()
        self._token = None
        super()._release()

    def _published_record(self) -> OwnerRecord:
        return super()._published_record()._replace(token=self._token, lease_duration=self._lease_duration)

    def _try_break_stale_lock(self) -> None:
        if (peer := self._read_peer()) is None:
            # Not a readable protocol 2 lease record: a partial write, a foreign or legacy protocol 1 marker, or the
            # strict sentinel. The base self-heal evicts a genuinely malformed marker once it ages past the grace
            # window and leaves a legitimate legacy or strict holder in place, so a corrupt marker no longer wedges
            # every lease contender until its own timeout.
            super()._try_break_stale_lock()
            return
        owner, mtime, ino = peer
        # A malformed, legacy or strict record is never reclaimed by age: only a peer that published a lease agreed to
        # be superseded by one. Raise the mismatch outside the read so the suppression cannot swallow it.
        if owner.mode != "lease":
            return
        if owner.lease_duration != self._lease_duration:
            msg = (
                f"{self.lock_file} holds a lease of {owner.lease_duration!r}s but this contender configured "
                f"{self._lease_duration!r}s; every contender for a path must agree on lease_duration"
            )
            raise LeaseSettingsMismatch(msg)
        # A break can fail for reasons a contender must ride out rather than raise on: a peer broke the marker first,
        # or Windows refuses to rename a file whose holder still has it open. Poll again instead.
        with suppress(OSError):
            # A dead or recycled owner is reclaimed at once; a live owner past its lease duration is superseded on the
            # schedule every contender agreed to.
            if owner_is_stale(owner.pid, owner.hostname, owner.start):
                break_lock_file(self.lock_file, mtime, ino)
                return
            if time.time() - mtime >= self._lease_duration:
                break_lock_file(self.lock_file, mtime, ino)

    def _read_peer(self) -> tuple[OwnerRecord, float, int] | None:
        with suppress(OSError, ValueError):
            content, mtime, ino = _read_lock_file(self.lock_file)
            if (owner := parse_marker(content)) is not None:
                return owner, mtime, ino
        return None

    def _start_heartbeat(self, fd: int, identity: tuple[int, int], token: str) -> None:
        self._heartbeat_stop = Event()
        self._heartbeat = Thread(
            target=self._refresh_until_stopped,
            args=(fd, identity, token),
            name=f"filelock-lease-{os.getpid()}",
            daemon=True,
        )
        self._heartbeat.start()

    def _stop_heartbeat(self) -> None:
        if (heartbeat := self._heartbeat) is None:
            return
        self._heartbeat_stop.set()
        self._heartbeat = None
        # on_compromise runs on the heartbeat thread and may release the lease, which lands back here.
        if heartbeat is not current_thread():
            heartbeat.join(timeout=self._heartbeat_interval)

    def _refresh_until_stopped(self, fd: int, identity: tuple[int, int], token: str) -> None:
        # The loop ends at the first loss of the claim, so the holder hears about it once. A transient filesystem
        # error (ESTALE / EIO on the NFS-style filesystems a lease targets) is not a loss: retry rather than raise a
        # false compromise. Report the claim unrefreshable only once failures have run long enough that a contender
        # could take it before the next success would land, a margin before the marker actually ages out, the way
        # restic declares a lock unrefreshable ahead of its stale time.
        last_success = time.monotonic()
        while not self._heartbeat_stop.wait(self._heartbeat_interval):
            outcome, error = self._refresh_claim(fd, identity, token)
            if outcome == "lost":
                return
            if outcome == "ok":
                last_success = time.monotonic()
            elif time.monotonic() - last_success >= self._lease_duration - self._heartbeat_interval:
                self._report_compromise("refresh-failed", error, token)
                return

    def _refresh_claim(self, fd: int, identity: tuple[int, int], token: str) -> tuple[_RefreshOutcome, OSError | None]:
        try:
            st = os.lstat(self.lock_file)
        except FileNotFoundError as error:
            self._report_compromise("marker-missing", error, token)
            return "lost", None
        except OSError as error:
            return "transient", error
        # A peer that took the expired claim replaced the marker, so the pathname now names its inode, not ours.
        if (st.st_dev, st.st_ino) != identity:
            self._report_compromise("owner-changed", None, token)
            return "lost", None
        try:
            touch(self.lock_file, fd=fd)
        except OSError as error:
            return "transient", error
        return "ok", None

    def _report_compromise(self, reason: CompromiseReason, error: OSError | None, token: str) -> None:
        # The token is the one this thread published, not self._token, which a release may already have cleared.
        self._compromise = LeaseCompromise(lock_file=self.lock_file, token=token, reason=reason, error=error)
        if self._on_compromise is not None:
            self._on_compromise(self._compromise)


__all__ = [
    "CompromiseReason",
    "LeaseCompromise",
    "SoftFileLease",
]
