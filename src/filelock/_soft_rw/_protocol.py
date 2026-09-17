"""
The generation-log protocol behind :class:`~filelock.SoftReadWriteLock`.

The lock's whole state is a sequence of immutable snapshots, ``gen/<N>``, each naming the writer and the readers that
hold the lock at generation ``N``. Every transition is one atomic publication of the next snapshot: a participant reads
the latest generation, derives the successor it wants, and links a fully written temporary file to ``gen/<N+1>``. The
link fails if a peer got there first, so the participant re-reads and tries again. No step is ever half done: a crash
leaves at most an unlinked temporary file, never a state that another participant must repair before it can proceed.

Liveness rests on a nonce, not a clock. Each participant keeps ``holders/<token>`` and rewrites its nonce every
heartbeat. A contender records the bytes it read there and the moment it read them on its own monotonic clock; a
member whose bytes have not changed for ``stale_threshold`` is evicted by committing a snapshot without it. Two hosts
never compare clocks, so a skewed file server or a suspended virtual machine cannot make a live holder look dead.

The generation number is monotonic and unique to one transition, so the generation at which a hold was granted fences
it: a resource that rejects anything carrying a lower generation than the highest it has accepted refuses a holder
that paused past its expiry and resumed.
"""

from __future__ import annotations

import os
import secrets
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Final, Literal, Protocol

from filelock._error import SoftFileLockProtocolError
from filelock._identity import host_name

if TYPE_CHECKING:
    from collections.abc import Callable

Mode = Literal["read", "write"]
HeartbeatOutcome = Literal["ok", "lost", "transient"]

_PROTOCOL: Final[str] = "filelock-rw/1"
_GENERATION_DIGITS: Final[int] = 20
_TOKEN_HEX_LENGTH: Final[int] = 32
#: A snapshot line is ``reader=`` plus a token, so this bounds a hostile record without capping real reader counts.
_MAX_RECORD_SIZE: Final[int] = 1 << 20
#: Snapshots older than this many generations behind the latest are removed by whoever commits. The window is also how
#: far ``latest`` probes past a gap before it trusts that nothing newer exists, so it bounds how stale a directory
#: listing may be before a participant could mistake an old generation for the head.
_RETAINED_GENERATIONS: Final[int] = 64
_GENERATIONS_DIRECTORY: Final[str] = "gen"
_HOLDERS_DIRECTORY: Final[str] = "holders"
_TEMPORARY_PREFIX: Final[str] = ".commit-"


class Files(Protocol):
    """The filesystem operations the protocol runs on; :mod:`._storage` implements them over :mod:`os`."""

    def read(self, path: str) -> bytes | None:
        """The file's bytes, ``None`` when it does not exist, or ``b""`` when it is not a regular file."""

    def create(self, path: str, data: bytes) -> None:
        """Create the file exclusively with the whole record written; raise ``FileExistsError`` when it exists."""

    def link(self, source: str, target: str) -> bool:
        """Hard-link *source* to *target* without replacing; ``True`` when *target* names *source*'s file afterwards."""

    def overwrite(self, path: str, data: bytes) -> bool:
        """Rewrite the file in place from offset zero; ``False`` when it no longer exists."""

    def unlink(self, path: str) -> None:
        """Remove the file, tolerating one that is already gone."""

    def listdir(self, path: str) -> list[str]:
        """The names in the directory, or an empty list when it does not exist."""

    def prepare(self, root: str) -> None:
        """Create the protocol directories under *root*, refusing anything at those paths that is not a directory."""


@dataclass(frozen=True)
class Snapshot:
    """The lock's state at one generation: which token writes, which tokens read."""

    generation: int
    writer: str | None
    readers: frozenset[str]

    @property
    def members(self) -> frozenset[str]:
        """Every token this generation names."""
        return self.readers if self.writer is None else self.readers | {self.writer}

    def encode(self) -> bytes:
        lines = [_PROTOCOL, f"generation={self.generation}"]
        if self.writer is not None:
            lines.append(f"writer={self.writer}")
        lines.extend(f"reader={token}" for token in sorted(self.readers))
        return "".join(f"{line}\n" for line in lines).encode("ascii")


def parse_snapshot(data: bytes) -> Snapshot | None:
    """The snapshot a ``gen/<N>`` record encodes, or ``None`` when the record is malformed."""
    if not data or len(data) > _MAX_RECORD_SIZE:
        return None
    try:
        lines = data.decode("ascii").split("\n")
    except UnicodeDecodeError:
        return None
    if lines[0] != _PROTOCOL or lines[-1]:
        return None
    generation: int | None = None
    writer: str | None = None
    readers: set[str] = set()
    for line in lines[1:-1]:
        key, _, value = line.partition("=")
        if (
            key == "generation"
            and generation is None
            and len(value) <= _GENERATION_DIGITS
            and value.isdigit()
            and str(int(value)) == value
        ):
            generation = int(value)
        elif key == "writer" and writer is None and is_token(value):
            writer = value
        elif key == "reader" and is_token(value) and value not in readers:
            readers.add(value)
        else:
            return None
    return None if generation is None else Snapshot(generation=generation, writer=writer, readers=frozenset(readers))


def is_token(value: str) -> bool:
    """Whether *value* is a claim token: exactly 32 lowercase hex digits."""
    return len(value) == _TOKEN_HEX_LENGTH and all(character in "0123456789abcdef" for character in value)


def new_token() -> str:
    """A fresh claim token."""
    return secrets.token_hex(_TOKEN_HEX_LENGTH // 2)


def encode_holder(token: str, nonce: str) -> bytes:
    """The ``holders/<token>`` record; only the nonce changes between heartbeats, so the length stays constant."""
    return f"{_PROTOCOL}\ntoken={token}\npid={os.getpid()}\nhost={host_name()}\nnonce={nonce}\n".encode("ascii")


class Ledger:
    """
    How long each observed value has stayed the same, measured on this observer's monotonic clock.

    A value that has not changed for ``stale_threshold`` belongs to a participant that stopped refreshing it. The clock
    is the observer's own and only ever measures the gap between two of its own observations, which is what lets two
    hosts agree on liveness without agreeing on the time.
    """

    def __init__(self, clock: Callable[[], float]) -> None:
        self._clock = clock
        self._seen: dict[str, tuple[bytes | None, float]] = {}

    def observe(self, key: str, value: bytes | None) -> float:
        """Record *value* for *key* and return how long it has been unchanged."""
        now = self._clock()
        if (seen := self._seen.get(key)) is None or seen[0] != value:
            self._seen[key] = (value, now)
            return 0.0
        return now - seen[1]

    def forget(self, key: str) -> None:
        self._seen.pop(key, None)


class GenerationLog:
    """The ordered snapshots under ``gen/``, found by listing and probing, advanced by linking the next one."""

    def __init__(self, files: Files, lock_file: str, root: str) -> None:
        self._files = files
        self._lock_file = lock_file
        self._directory = Path(root, _GENERATIONS_DIRECTORY)
        self._latest: Snapshot | None = None

    def latest(self) -> Snapshot:
        """
        The newest snapshot.

        Lists the directory on every call, since opening it is what makes an NFS client revalidate what it has cached
        about the names inside, then starts from the newest readable generation the listing or memory names and probes
        forward by name across the retained window. A listing served stale, a generation compacted between the listing
        and the read, and a hole left by a committer that died before it compacted are all covered by the probe; only a
        listing that names generations of which none can be read is refused, because that is a client so far behind
        the log that nothing it reads can be trusted.
        """
        listed = self._list()
        if (start := self._start(listed)) is None:
            # Everything the listing named is gone: peers compacted past it while this client looked. One more listing
            # settles whether the directory is genuinely empty or this client cannot see the head at all.
            listed = self._list()
            if (start := self._start(listed)) is None:
                if listed:
                    raise SoftFileLockProtocolError(self._lock_file, None, "generation log names no readable snapshot")
                start = Snapshot(generation=0, writer=None, readers=frozenset())
        latest = start
        generation = start.generation
        gap = 0
        while gap < _RETAINED_GENERATIONS:
            generation += 1
            if (snapshot := self._read(generation)) is None:
                gap += 1
                continue
            latest = snapshot
            gap = 0
        self._latest = latest
        return latest

    def _list(self) -> list[int]:
        listed = sorted(
            int(name)
            for name in self._files.listdir(str(self._directory))
            if name.isdigit() and len(name) == _GENERATION_DIGITS
        )
        for generation in listed[:-_RETAINED_GENERATIONS]:
            self._files.unlink(self._path(generation))
        return listed[-_RETAINED_GENERATIONS:]

    def _start(self, listed: list[int]) -> Snapshot | None:
        remembered = self._latest
        for generation in reversed(listed):
            if remembered is not None and remembered.generation >= generation:
                return remembered
            if (snapshot := self._read(generation)) is not None:
                return snapshot
        return remembered

    def _read(self, generation: int) -> Snapshot | None:
        if (data := self._files.read(self._path(generation))) is None:
            return None
        if (snapshot := parse_snapshot(data)) is None or snapshot.generation != generation:
            name = self._name(generation)
            raise SoftFileLockProtocolError(self._lock_file, name, "malformed generation record")
        return snapshot

    def commit(self, successor: Snapshot) -> bool:
        """
        Publish *successor* as the next generation; ``False`` when a peer published that generation first.

        The record is complete before it gets its public name, and a no-replace hard link is the compare-and-swap: only
        one link to ``gen/<N+1>`` can ever succeed. The link's return code is not trusted, because an NFS retry can
        report a link that did land as failed; the file's identity after the call is what decides.
        """
        temporary = str(self._directory / f"{_TEMPORARY_PREFIX}{new_token()}")
        self._files.create(temporary, successor.encode())
        try:
            linked = self._files.link(temporary, self._path(successor.generation))
        finally:
            self._files.unlink(temporary)
        if not linked:
            return False
        self._latest = successor
        self._files.unlink(self._path(successor.generation - _RETAINED_GENERATIONS - 1))
        return True

    def _path(self, generation: int) -> str:
        return str(self._directory / self._name(generation))

    @staticmethod
    def _name(generation: int) -> str:
        return f"{generation:0{_GENERATION_DIGITS}d}"


class Participant:
    """
    One acquisition, from publishing a claim through holding the lock to leaving.

    ``advance`` is polled until it reports the lock granted, ``heartbeat`` runs on the holder's schedule, and ``leave``
    commits the participant out. None of them sleep: the caller owns the deadline and the poll interval. The ledger and
    the log outlive one acquisition: how long a peer's record has stayed unchanged is knowledge every attempt against
    the same lock shares, or a caller whose attempts are each shorter than the stale threshold could never evict.
    """

    def __init__(  # ruff:ignore[too-many-arguments]  # one value per protocol input; the lock builds this once per acquisition
        self,
        files: Files,
        root: str,
        mode: Mode,
        *,
        stale_threshold: float,
        clock: Callable[[], float],
        ledger: Ledger,
        log: GenerationLog,
    ) -> None:
        self.token: Final[str] = new_token()
        self.mode: Final[Mode] = mode
        self.generation: int | None = None
        self._files = files
        self._root = root
        self._stale_threshold = stale_threshold
        self._clock = clock
        self._ledger = ledger
        self._log = log
        self._holder = self._holder_path(self.token)
        self._entered = False
        self._next_sweep = clock()

    def publish(self) -> None:
        """Create the holder record peers watch for liveness; it exists before the token appears in any snapshot."""
        self._files.prepare(self._root)
        self._files.create(self._holder, encode_holder(self.token, new_token()))

    def advance(self) -> bool:
        """
        Take the next step toward holding the lock; ``True`` once the lock is granted.

        A reader enters as soon as no live writer is named. A writer enters as soon as no live writer is named, which
        blocks every new reader, then waits for the named readers to leave. Stale members are evicted in the same commit
        that admits this participant, and a writer's final admission is itself a commit, so no admission can rest on a
        snapshot a peer is about to supersede.
        """
        # A lost race is not a reason to wait: re-read and try again before handing the poll interval back to the
        # caller.
        for _ in range(4):
            latest = self._log.latest()
            self._keep_claim_fresh(latest)
            self._sweep(latest)
            stale = frozenset(token for token in latest.members - {self.token} if self._is_stale(token))
            writer = None if latest.writer in stale else latest.writer
            readers = latest.readers - stale
            if not self._entered:
                if writer is not None:
                    # A live writer blocks entry in both modes; only an eviction is worth committing.
                    if stale:
                        self._commit(latest, writer, readers, stale)
                    return False
                writer, readers = (self.token, readers) if self.mode == "write" else (None, readers | {self.token})
            granted = self.mode == "read" or not readers
            if self._entered and not stale and not granted:
                return False
            if (generation := self._commit(latest, writer, readers, stale)) is None:
                continue
            self._entered = True
            if granted:
                self.generation = generation
            return granted
        return False

    def _keep_claim_fresh(self, latest: Snapshot) -> None:
        # The snapshot is the truth about membership: a commit the filesystem reported lost may have landed, and a peer
        # may have evicted this participant while it waited to drain, so both directions are taken from it. A wait can
        # outlast the stale threshold, so the nonce changes on every poll; a peer then never reads a patient contender
        # as a corpse. A record that has gone missing was taken by an evictor or a sweeper that did.
        self._entered = self.token in latest.members
        if not self._refresh_nonce():
            with suppress(FileExistsError):
                self._files.create(self._holder, encode_holder(self.token, new_token()))

    def _sweep(self, latest: Snapshot) -> None:
        # A participant that crashed before its first commit leaves a record no snapshot names, and a commit that
        # crashed between creating and unlinking its temporary file leaves that too. Neither blocks anyone, so they are
        # collected on the same unchanged-for-a-threshold rule as a member, and only every half threshold at most.
        if (now := self._clock()) < self._next_sweep:
            return
        self._next_sweep = now + self._stale_threshold / 2
        holders = Path(self._root, _HOLDERS_DIRECTORY)
        for name in self._files.listdir(str(holders)):
            if name != self.token and name not in latest.members and is_token(name):
                self._collect(f"holder:{name}", str(holders / name))
        generations = Path(self._root, _GENERATIONS_DIRECTORY)
        for name in self._files.listdir(str(generations)):
            if name.startswith(_TEMPORARY_PREFIX):
                self._collect(f"temporary:{name}", str(generations / name))

    def _collect(self, key: str, path: str) -> None:
        if self._ledger.observe(key, self._files.read(path)) >= self._stale_threshold:
            self._files.unlink(path)
            self._ledger.forget(key)

    def _commit(
        self, latest: Snapshot, writer: str | None, readers: frozenset[str], stale: frozenset[str]
    ) -> int | None:
        successor = Snapshot(generation=latest.generation + 1, writer=writer, readers=readers)
        if not self._log.commit(successor):
            return None
        for token in stale:
            self._files.unlink(self._holder_path(token))
            self._ledger.forget(token)
        return successor.generation

    def _is_stale(self, token: str) -> bool:
        return self._ledger.observe(token, self._files.read(self._holder_path(token))) >= self._stale_threshold

    def _holder_path(self, token: str) -> str:
        return str(Path(self._root, _HOLDERS_DIRECTORY, token))

    def _refresh_nonce(self) -> bool:
        return self._files.overwrite(self._holder, encode_holder(self.token, new_token()))

    def heartbeat(self) -> tuple[HeartbeatOutcome, OSError | None]:
        """
        Refresh the nonce and confirm the latest snapshot still names this participant.

        ``lost`` means a peer evicted this participant or removed its holder record: the lock is no longer held, and the
        holder must stop using what it protects. ``transient`` is a filesystem error refreshing the nonce, worth
        retrying on the next tick. A failure to read the log or to sweep decides nothing about this holder's liveness,
        so it is not counted against it.
        """
        try:
            if not self._refresh_nonce():
                return "lost", None
        except OSError as error:
            return "transient", error
        try:
            latest = self._log.latest()
            self._sweep(latest)
        except OSError:
            return "ok", None
        return ("ok" if self.token in latest.members else "lost"), None

    def leave(self) -> None:
        """Commit this participant out of the latest snapshot, then remove its holder record."""
        while self.token in (latest := self._log.latest()).members:
            successor = Snapshot(
                generation=latest.generation + 1,
                writer=None if latest.writer == self.token else latest.writer,
                readers=latest.readers - {self.token},
            )
            if self._log.commit(successor):
                break
        self._entered = False
        self.generation = None
        self._files.unlink(self._holder)


__all__ = [
    "Files",
    "GenerationLog",
    "HeartbeatOutcome",
    "Ledger",
    "Mode",
    "Participant",
    "Snapshot",
    "encode_holder",
    "is_token",
    "new_token",
    "parse_snapshot",
]
