"""
The generation-log protocol: record formats, the ledger, and ``GenerationLog``/``Participant`` behavior, plus a model
check of the whole protocol under an adversarial scheduler.

The direct tests below exercise ``Snapshot``, ``Ledger``, ``GenerationLog``, and ``Participant`` against a real
filesystem. The model check that follows runs every participant on its own thread over an in-memory filesystem, and
every filesystem call hands the turn back to a seeded scheduler that picks which participant runs next, advances a fake
clock, and sometimes kills the participant it picked. That interleaves the protocol at the granularity of single
filesystem operations, including inside a commit, and a crash at any of those points leaves exactly what a dead process
would leave on disk.
"""

from __future__ import annotations

import os
import random
import threading
from dataclasses import dataclass, field
from errno import EIO
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Final, Literal

import pytest

from filelock import SoftFileLockProtocolError
from filelock._soft_rw._protocol import GenerationLog, Ledger, Participant, Snapshot, encode_holder, parse_snapshot
from filelock._soft_rw._storage import OsFiles

if TYPE_CHECKING:
    from collections.abc import Callable

    from pytest_mock import MockerFixture

_FIRST = "0123456789abcdef0123456789abcdef"
_SECOND = "fedcba9876543210fedcba9876543210"


def _participant(
    files: OsFiles,
    lock_file: str,
    mode: Literal["read", "write"],
    *,
    stale_threshold: float = 100,
    clock: Callable[[], float] = lambda: 0.0,
) -> Participant:
    # Each participant gets its own ledger and log, the way separate processes do.
    root = f"{lock_file}.rw"
    return Participant(
        files,
        root,
        mode,
        stale_threshold=stale_threshold,
        clock=clock,
        ledger=Ledger(clock),
        log=GenerationLog(files, lock_file, root),
    )


@pytest.mark.parametrize(
    "snapshot",
    [
        pytest.param(Snapshot(generation=0, writer=None, readers=frozenset()), id="empty"),
        pytest.param(Snapshot(generation=7, writer=_FIRST, readers=frozenset()), id="writer"),
        pytest.param(Snapshot(generation=12, writer=None, readers=frozenset({_FIRST, _SECOND})), id="readers"),
    ],
)
def test_snapshot_round_trips(snapshot: Snapshot) -> None:
    assert parse_snapshot(snapshot.encode()) == snapshot


@pytest.mark.parametrize(
    "data",
    [
        pytest.param(b"", id="empty"),
        pytest.param(b"x" * ((1 << 20) + 1), id="oversized"),
        pytest.param("filelock-rw/1\ngeneration=1\nreader=é\n".encode(), id="non-ascii"),
        pytest.param(b"filelock-rw/2\ngeneration=1\n", id="unknown-protocol"),
        pytest.param(b"filelock-rw/1\ngeneration=1", id="no-trailing-newline"),
        pytest.param(b"filelock-rw/1\n", id="no-generation"),
        pytest.param(b"filelock-rw/1\ngeneration=01\n", id="leading-zero"),
        pytest.param(b"filelock-rw/1\ngeneration=1\ngeneration=2\n", id="two-generations"),
        pytest.param(f"filelock-rw/1\ngeneration=1\nwriter={_FIRST}\nwriter={_SECOND}\n".encode(), id="two-writers"),
        pytest.param(f"filelock-rw/1\ngeneration=1\nreader={_FIRST}\nreader={_FIRST}\n".encode(), id="reader-twice"),
        pytest.param(b"filelock-rw/1\ngeneration=1\nwriter=short\n", id="bad-token"),
        pytest.param(b"filelock-rw/1\ngeneration=1\ncolor=blue\n", id="unknown-key"),
        pytest.param(b"filelock-rw/1\ngeneration=1\nno separator\n", id="no-separator"),
    ],
)
def test_malformed_snapshots_parse_to_none(data: bytes) -> None:
    assert parse_snapshot(data) is None


def test_holder_record_keeps_its_length_across_nonces() -> None:
    first = encode_holder(_FIRST, _SECOND)
    second = encode_holder(_FIRST, _FIRST)
    assert len(first) == len(second)
    assert first != second
    assert f"pid={os.getpid()}".encode() in first


def test_ledger_measures_how_long_a_value_stays_unchanged() -> None:
    clock = [10.0]
    ledger = Ledger(lambda: clock[0])
    assert ledger.observe("a", b"1") == pytest.approx(0.0)
    clock[0] = 12.5
    assert ledger.observe("a", b"1") == pytest.approx(2.5)
    assert ledger.observe("a", b"2") == pytest.approx(0.0)
    clock[0] = 13.0
    assert ledger.observe("a", None) == pytest.approx(0.0)
    clock[0] = 14.0
    assert ledger.observe("a", None) == pytest.approx(1.0)
    ledger.forget("a")
    assert ledger.observe("a", None) == pytest.approx(0.0)


def test_log_starts_empty(tmp_path: Path) -> None:
    lock_file = str(tmp_path / "x.lock")
    log = GenerationLog(OsFiles(lock_file), lock_file, f"{lock_file}.rw")
    assert log.latest() == Snapshot(generation=0, writer=None, readers=frozenset())


@pytest.mark.requires_hard_links
def test_commit_refuses_a_generation_that_exists(tmp_path: Path) -> None:
    lock_file = str(tmp_path / "x.lock")
    files = OsFiles(lock_file)
    root = f"{lock_file}.rw"
    files.prepare(root)
    first = GenerationLog(files, lock_file, root)
    second = GenerationLog(files, lock_file, root)
    assert first.latest().generation == 0
    assert second.latest().generation == 0
    assert first.commit(Snapshot(generation=1, writer=_FIRST, readers=frozenset()))
    assert not second.commit(Snapshot(generation=1, writer=_SECOND, readers=frozenset()))
    assert second.latest().writer == _FIRST
    assert sorted(entry.name for entry in Path(root, "gen").iterdir()) == [f"{1:020d}"]


@pytest.mark.requires_hard_links
def test_rescan_skips_a_generation_removed_after_listing(tmp_path: Path, mocker: MockerFixture) -> None:
    # A listing can name a generation a peer compacts before it is read; the next older one still stands in.
    lock_file = str(tmp_path / "x.lock")
    files = OsFiles(lock_file)
    root = f"{lock_file}.rw"
    files.prepare(root)
    log = GenerationLog(files, lock_file, root)
    assert log.commit(Snapshot(generation=1, writer=None, readers=frozenset()))
    assert log.commit(Snapshot(generation=2, writer=_FIRST, readers=frozenset()))
    real_read = OsFiles.read
    latest_path = str(Path(root, "gen", f"{2:020d}"))
    mocker.patch.object(OsFiles, "read", side_effect=lambda path: None if path == latest_path else real_read(path))
    fresh = GenerationLog(files, lock_file, root)
    assert fresh.latest().generation == 1


def test_malformed_generation_raises(tmp_path: Path) -> None:
    lock_file = str(tmp_path / "x.lock")
    files = OsFiles(lock_file)
    root = f"{lock_file}.rw"
    files.prepare(root)
    Path(root, "gen", f"{1:020d}").write_bytes(b"filelock-rw/1\ngeneration=5\n")
    log = GenerationLog(files, lock_file, root)
    with pytest.raises(SoftFileLockProtocolError, match="malformed generation record") as caught:
        log.latest()
    assert caught.value.claim_name == f"{1:020d}"


def test_leave_without_entering_only_drops_the_record(tmp_path: Path) -> None:
    lock_file = str(tmp_path / "x.lock")
    participant = _participant(OsFiles(lock_file), lock_file, "read", stale_threshold=1)
    participant.publish()
    assert Path(f"{lock_file}.rw", "holders", participant.token).exists()
    participant.leave()
    assert not Path(f"{lock_file}.rw", "holders", participant.token).exists()
    assert participant.generation is None


@pytest.mark.requires_hard_links
def test_leave_retries_a_commit_a_peer_won(tmp_path: Path, mocker: MockerFixture) -> None:
    # A peer can publish the next generation between leave() reading the latest one and linking its successor; the
    # holder then re-reads and commits itself out of whatever the peer published.
    lock_file = str(tmp_path / "x.lock")
    files = OsFiles(lock_file)
    root = f"{lock_file}.rw"
    reader = _participant(files, lock_file, "read", stale_threshold=100)
    reader.publish()
    assert reader.advance()
    real_commit = GenerationLog.commit
    lost = []

    def commit_after_a_peer(log: GenerationLog, successor: Snapshot) -> bool:
        if not lost:
            lost.append(successor)
            peer = GenerationLog(files, lock_file, root)
            still_reading = successor.readers | {reader.token}
            assert real_commit(peer, Snapshot(generation=successor.generation, writer=None, readers=still_reading))
        return real_commit(log, successor)

    mocker.patch.object(GenerationLog, "commit", autospec=True, side_effect=commit_after_a_peer)
    reader.leave()
    latest = GenerationLog(files, lock_file, root).latest()
    assert latest.members == frozenset()
    assert latest.generation == lost[0].generation + 1


@pytest.mark.requires_hard_links
def test_waiting_contender_republishes_a_swept_record(tmp_path: Path) -> None:
    # A record removed out from under a contender (a sweeper that mistook it, an operator) comes back on its next poll,
    # so peers always have a record to watch by the time the contender is admitted.
    lock_file = str(tmp_path / "x.lock")
    files = OsFiles(lock_file)
    root = f"{lock_file}.rw"
    writer = _participant(files, lock_file, "write", stale_threshold=100)
    writer.publish()
    assert writer.advance()
    reader = _participant(files, lock_file, "read", stale_threshold=100)
    reader.publish()
    record = Path(root, "holders", reader.token)
    record.unlink()
    assert not reader.advance()
    assert record.exists()


@pytest.mark.requires_hard_links
def test_waiting_contender_keeps_an_undeletable_record(tmp_path: Path, mocker: MockerFixture) -> None:
    # Windows can leave the record in place while refusing the rewrite; the contender carries on.
    lock_file = str(tmp_path / "x.lock")
    files = OsFiles(lock_file)
    root = f"{lock_file}.rw"
    reader = _participant(files, lock_file, "read", stale_threshold=100)
    reader.publish()
    mocker.patch.object(OsFiles, "overwrite", return_value=False)
    assert reader.advance()
    assert Path(root, "holders", reader.token).exists()


@pytest.mark.requires_hard_links
def test_blocked_contender_still_evicts_stale_readers(tmp_path: Path) -> None:
    # A live writer blocks entry, but a dead reader named beside it is evicted anyway so the writer can drain.
    lock_file = str(tmp_path / "x.lock")
    files = OsFiles(lock_file)
    root = f"{lock_file}.rw"
    clock = [0.0]
    dead = _participant(files, lock_file, "read", stale_threshold=1, clock=lambda: clock[0])
    dead.publish()
    assert dead.advance()
    writer = _participant(files, lock_file, "write", stale_threshold=1, clock=lambda: clock[0])
    writer.publish()
    assert not writer.advance()
    contender = _participant(files, lock_file, "read", stale_threshold=1, clock=lambda: clock[0])
    contender.publish()
    assert not contender.advance()
    clock[0] = 5.0
    assert writer.heartbeat() == ("ok", None)  # keeps its own nonce fresh, so only the dead reader goes stale
    assert not contender.advance()
    latest = GenerationLog(files, lock_file, root).latest()
    assert latest.writer == writer.token
    assert latest.readers == frozenset()


@pytest.mark.requires_hard_links
def test_writer_admission_after_the_last_reader_leaves_is_a_commit(tmp_path: Path) -> None:
    # The final grant must publish a generation of its own: a grant read off a snapshot a peer is about to supersede
    # would let an evictor and the resumed writer both hold.
    lock_file = str(tmp_path / "x.lock")
    files = OsFiles(lock_file)
    reader = _participant(files, lock_file, "read")
    reader.publish()
    assert reader.advance()
    writer = _participant(files, lock_file, "write")
    writer.publish()
    assert not writer.advance()
    reader.leave()
    left_at = GenerationLog(files, lock_file, f"{lock_file}.rw").latest().generation
    assert writer.advance()
    assert writer.generation == left_at + 1


@pytest.mark.requires_hard_links
def test_a_commit_reported_lost_that_landed_is_recognized(tmp_path: Path, mocker: MockerFixture) -> None:
    # A link that landed but whose identity check failed must not leave the writer blocking on its own token.
    lock_file = str(tmp_path / "x.lock")
    files = OsFiles(lock_file)
    writer = _participant(files, lock_file, "write")
    writer.publish()
    real_link = OsFiles.link
    denied = []

    def link_then_deny(self: OsFiles, source: str, target: str) -> bool:
        landed = real_link(self, source, target)
        if landed and not denied:
            denied.append(target)
            return False
        return landed

    mocker.patch.object(OsFiles, "link", autospec=True, side_effect=link_then_deny)
    assert writer.advance()
    # The denied commit entered it at generation 1; the admission it then recognized is a commit of its own.
    assert writer.generation == 2
    assert len(denied) == 1
    assert GenerationLog(files, lock_file, f"{lock_file}.rw").latest().writer == writer.token


@pytest.mark.requires_hard_links
def test_latest_probes_across_a_compaction_hole(tmp_path: Path) -> None:
    lock_file = str(tmp_path / "x.lock")
    files = OsFiles(lock_file)
    root = f"{lock_file}.rw"
    files.prepare(root)
    log = GenerationLog(files, lock_file, root)
    for generation in range(1, 6):
        assert log.commit(Snapshot(generation=generation, writer=None, readers=frozenset()))
    Path(root, "gen", f"{3:020d}").unlink()
    behind = GenerationLog(files, lock_file, root)
    assert behind.latest().generation == 5


def test_a_listing_naming_only_compacted_generations_fails_closed(tmp_path: Path, mocker: MockerFixture) -> None:
    # A client that can see names but read none of them is behind the log by more than the retained window; treating
    # that as an empty log would restart the sequence and fork it.
    lock_file = str(tmp_path / "x.lock")
    files = OsFiles(lock_file)
    root = f"{lock_file}.rw"
    files.prepare(root)
    mocker.patch.object(OsFiles, "listdir", return_value=[f"{generation:020d}" for generation in (4, 5)])
    with pytest.raises(SoftFileLockProtocolError, match="names no readable snapshot"):
        GenerationLog(files, lock_file, root).latest()


@pytest.mark.requires_hard_links
def test_a_listing_compacted_from_under_the_reader_is_taken_again(tmp_path: Path, mocker: MockerFixture) -> None:
    # Peers can compact every generation a listing named before this client reads one; a second listing then finds
    # the head rather than failing closed on the first.
    lock_file = str(tmp_path / "x.lock")
    files = OsFiles(lock_file)
    root = f"{lock_file}.rw"
    files.prepare(root)
    log = GenerationLog(files, lock_file, root)
    assert log.commit(Snapshot(generation=1, writer=_FIRST, readers=frozenset()))
    real_listdir = OsFiles.listdir
    listings = iter([[f"{generation:020d}" for generation in (4, 5)]])
    mocker.patch.object(OsFiles, "listdir", side_effect=lambda path: next(listings, None) or real_listdir(path))
    assert GenerationLog(files, lock_file, root).latest().writer == _FIRST


@pytest.mark.requires_hard_links
def test_heartbeat_ignores_a_broken_successor_record(tmp_path: Path) -> None:
    # A record nobody can parse breaks every acquire, and that is its own error; it says nothing about whether this
    # holder is alive, so the heartbeat must keep the nonce fresh rather than stop and hand the lock to a peer.
    lock_file = str(tmp_path / "x.lock")
    files = OsFiles(lock_file)
    writer = _participant(files, lock_file, "write")
    writer.publish()
    assert writer.advance()
    Path(f"{lock_file}.rw", "gen", f"{2:020d}").write_bytes(b"garbage\n")
    assert writer.heartbeat() == ("ok", None)


@pytest.mark.requires_hard_links
def test_heartbeat_reports_the_refresh_error(tmp_path: Path, mocker: MockerFixture) -> None:
    lock_file = str(tmp_path / "x.lock")
    files = OsFiles(lock_file)
    writer = _participant(files, lock_file, "write")
    writer.publish()
    assert writer.advance()
    error = OSError(EIO, "Input/output error")
    mocker.patch.object(OsFiles, "overwrite", side_effect=error)
    assert writer.heartbeat() == ("transient", error)


_STALE_THRESHOLD: Final[float] = 1.0
_HEARTBEAT: Final[float] = 0.3
_POLL: Final[float] = 0.1
#: What one filesystem operation costs. Small against the heartbeat so a live participant's refreshes stay well inside
#: the stale threshold however the scheduler orders its operations, as they do on a real filesystem.
_STEP: Final[float] = 0.001
#: A runnable participant is scheduled again before this much fake time passes, so only a crashed one goes stale.
_FAIRNESS: Final[float] = 8 * _STEP
_ACQUIRE_BUDGET: Final[float] = 120.0
_ROOT: Final[str] = "/lock.rw"


class _Crashed(BaseException):
    """Raised inside a participant the scheduler killed; it exits at once and never touches the filesystem again."""


@dataclass
class _Scheduler:
    """
    Discrete-event scheduler: a parked participant is runnable once its wake time has passed, the clock only jumps when
    nothing is runnable, and every scheduled operation costs one step. A runnable participant waits at most
    ``_FAIRNESS`` before it is picked, so only a crashed participant can ever look stale.
    """

    seed: int
    crash_probability: float
    max_crashes: int
    clock: float = 0.0
    crashes: int = 0
    _random: random.Random = field(init=False)
    _condition: threading.Condition = field(default_factory=threading.Condition)
    _expected: int = 0
    _live: set[int] = field(default_factory=set)
    _parked: dict[int, float] = field(default_factory=dict)
    _turn: int | None = None
    _crashed: set[int] = field(default_factory=set)

    def __post_init__(self) -> None:
        self._random = random.Random(self.seed)  # ruff: ignore[suspicious-non-cryptographic-random-usage]  # a seeded schedule is the point, not entropy

    def expect(self) -> None:
        # Counted before the thread starts, so the first step cannot conclude that nobody is left to run.
        with self._condition:
            self._expected += 1

    def register(self) -> None:
        with self._condition:
            self._expected -= 1
            self._live.add(threading.get_ident())
            self._condition.notify_all()

    def yield_turn(self, wake_at: float | None = None) -> None:
        me = threading.get_ident()
        with self._condition:
            self._parked[me] = self.clock if wake_at is None else wake_at
            self._condition.notify_all()
            while self._turn != me:
                self._condition.wait()
            self._turn = None
            crashed = me in self._crashed
        if crashed:
            raise _Crashed

    def sleep(self, seconds: float) -> None:
        self.yield_turn(wake_at=self.clock + seconds)

    def finish(self) -> None:
        with self._condition:
            self._live.discard(threading.get_ident())
            self._parked.pop(threading.get_ident(), None)
            self._condition.notify_all()

    def step(self) -> bool:
        """Run one participant for one filesystem operation; ``False`` once every participant has finished."""
        with self._condition:
            while self._expected or self._turn is not None or set(self._parked) != self._live:
                self._condition.wait()
            if not self._live:
                return False
            if not (runnable := [ident for ident, wake_at in self._parked.items() if wake_at <= self.clock]):
                self.clock = min(self._parked.values())
                runnable = [ident for ident, wake_at in self._parked.items() if wake_at <= self.clock]
            starving = [ident for ident in runnable if self.clock - self._parked[ident] >= _FAIRNESS]
            chosen = self._random.choice(sorted(starving or runnable))
            del self._parked[chosen]
            self.clock += _STEP
            if self.crashes < self.max_crashes and self._random.random() < self.crash_probability:
                self._crashed.add(chosen)
                self.crashes += 1
            self._turn = chosen
            self._condition.notify_all()
        return True


class _MemoryFiles:
    """An in-memory :class:`~filelock._soft_rw._protocol.Files` whose every call is one scheduler step."""

    def __init__(self, scheduler: _Scheduler, *, stale_listings: float = 0.0) -> None:
        self._scheduler = scheduler
        self.files: dict[str, bytes] = {}
        # An NFS client can serve a directory listing that predates a peer's commit; with this probability a listing
        # comes from some earlier point in the tree's history rather than from now.
        self._stale_listings = stale_listings
        self._random = random.Random(scheduler.seed + 1)  # ruff: ignore[suspicious-non-cryptographic-random-usage]  # a seeded schedule, not entropy
        self._history: list[list[str]] = [[]]

    def _record(self) -> None:
        self._history.append(sorted(self.files))
        del self._history[:-128]

    def read(self, path: str) -> bytes | None:
        self._scheduler.yield_turn()
        return self.files.get(_key(path))

    def create(self, path: str, data: bytes) -> None:
        self._scheduler.yield_turn()
        if _key(path) in self.files:  # pragma: no cover  # every name created here is a fresh token
            raise FileExistsError(path)
        self.files[_key(path)] = data
        self._record()

    def link(self, source: str, target: str) -> bool:
        self._scheduler.yield_turn()
        if _key(target) in self.files or _key(source) not in self.files:
            return False
        self.files[_key(target)] = self.files[_key(source)]
        self._record()
        return True

    def overwrite(self, path: str, data: bytes) -> bool:
        self._scheduler.yield_turn()
        if (
            _key(path) not in self.files
        ):  # pragma: no cover  # only a crashed participant loses its record, and it never writes again
            return False
        self.files[_key(path)] = data
        self._record()
        return True

    def unlink(self, path: str) -> None:
        self._scheduler.yield_turn()
        self.files.pop(_key(path), None)
        self._record()

    def listdir(self, path: str) -> list[str]:
        self._scheduler.yield_turn()
        names = (
            self._random.choice(self._history) if self._random.random() < self._stale_listings else sorted(self.files)
        )
        return [PurePosixPath(name).name for name in names if str(PurePosixPath(name).parent) == _key(path)]

    def prepare(self, root: str) -> None:  # ruff:ignore[unused-method-argument]  # nothing to create in memory
        self._scheduler.yield_turn()


def _key(path: str) -> str:
    # The protocol joins paths with the host separator; the in-memory tree keys them the POSIX way on every platform.
    return path.replace("\\", "/")


@dataclass
class _Outcome:
    mode: Literal["read", "write"]
    granted_at: int | None = None
    crashed: bool = False
    lost: bool = False
    stuck: bool = False


class _Model:
    def __init__(self, scheduler: _Scheduler, *, stale_listings: float = 0.0) -> None:
        self.scheduler = scheduler
        self.files = _MemoryFiles(scheduler, stale_listings=stale_listings)
        self.holding: dict[str, Literal["read", "write"]] = {}
        self.last_granted = 0
        self.outcomes: list[_Outcome] = []
        self.threads: list[threading.Thread] = []
        self.violations: list[str] = []

    def add(self, mode: Literal["read", "write"], hold_beats: int) -> None:
        outcome = _Outcome(mode)
        self.outcomes.append(outcome)
        self.scheduler.expect()
        self.threads.append(threading.Thread(target=self._run, args=(outcome, hold_beats), daemon=True))

    def _run(self, outcome: _Outcome, hold_beats: int) -> None:
        self.scheduler.register()
        participant = Participant(
            self.files,
            _ROOT,
            outcome.mode,
            stale_threshold=_STALE_THRESHOLD,
            clock=lambda: self.scheduler.clock,
            ledger=Ledger(lambda: self.scheduler.clock),
            log=GenerationLog(self.files, "/lock", _ROOT),
        )
        try:
            participant.publish()
            deadline = self.scheduler.clock + _ACQUIRE_BUDGET
            while not participant.advance():
                if (
                    self.scheduler.clock >= deadline
                ):  # pragma: no cover  # a liveness regression, reported by the assertion
                    outcome.stuck = True
                    return
                self.scheduler.sleep(_POLL)
            outcome.granted_at = participant.generation
            self._enter(participant.token, outcome.mode, outcome.granted_at)
            for _ in range(hold_beats):
                self.scheduler.sleep(_HEARTBEAT)
                if (
                    participant.heartbeat()[0] != "ok"
                ):  # pragma: no cover  # a live holder evicted: a regression the test reports
                    outcome.lost = True
                    break
            self.holding.pop(participant.token, None)
            participant.leave()
        except _Crashed:
            outcome.crashed = True
            self.holding.pop(participant.token, None)
        finally:
            self.scheduler.finish()

    def _enter(self, token: str, mode: Literal["read", "write"], granted_at: int | None) -> None:
        # Recorded from the participant's thread while it holds the turn, so the check sees a consistent set.
        writers = [held for held in self.holding.values() if held == "write"]
        if (
            granted_at is None or granted_at <= self.last_granted
        ):  # pragma: no cover  # a forked log, reported by the assertion
            self.violations.append(f"grant at generation {granted_at} after generation {self.last_granted}")
        else:
            self.last_granted = granted_at
        if mode == "write" and self.holding:  # pragma: no cover  # an exclusion regression, reported by the assertion
            self.violations.append(
                f"writer granted at {self.scheduler.clock:.2f} while {sorted(self.holding.values())}"
            )
        elif mode == "read" and writers:  # pragma: no cover  # an exclusion regression, reported by the assertion
            self.violations.append(f"reader granted at {self.scheduler.clock:.2f} while a writer holds")
        self.holding[token] = mode

    def run(self) -> None:
        for thread in self.threads:
            thread.start()
        while self.scheduler.step():
            pass
        for thread in self.threads:
            thread.join(timeout=30)
            assert not thread.is_alive()


@pytest.mark.parametrize(
    ("seed", "crash_probability", "stale_listings"),
    [
        pytest.param(
            seed,
            probability,
            stale,
            id=f"seed{seed}-{'crashes' if probability else 'clean'}-{'stale-listings' if stale else 'fresh-listings'}",
        )
        for seed in range(24)
        for probability in (0.0 if seed % 3 == 0 else 0.02,)
        for stale in (0.3 if seed % 2 else 0.0,)
    ],
)
@pytest.mark.timeout(120)
def test_model_never_overlaps_and_always_progresses(seed: int, crash_probability: float, stale_listings: float) -> None:
    scheduler = _Scheduler(seed=seed, crash_probability=crash_probability, max_crashes=3)
    model = _Model(scheduler, stale_listings=stale_listings)
    rng = random.Random(seed)  # ruff: ignore[suspicious-non-cryptographic-random-usage]  # a seeded mix of hold lengths, not entropy
    for index in range(8):
        model.add("write" if index % 3 == 0 else "read", hold_beats=rng.randint(1, 4))
    model.run()

    assert model.violations == []
    survivors = [outcome for outcome in model.outcomes if not outcome.crashed]
    assert [outcome for outcome in survivors if outcome.stuck] == []
    assert [outcome for outcome in survivors if outcome.lost] == []
    assert all(outcome.granted_at is not None for outcome in survivors)
    assert scheduler.crashes == sum(outcome.crashed for outcome in model.outcomes)


@pytest.mark.parametrize("seed", range(6))
@pytest.mark.timeout(120)
def test_model_janitor_clears_every_crashed_holder(seed: int) -> None:
    # After the crashes, one more writer must be able to enter by evicting whatever the dead left behind, and leave the
    # log naming nobody and the holders directory holding nobody.
    scheduler = _Scheduler(seed=seed, crash_probability=0.05, max_crashes=4)
    model = _Model(scheduler)
    for index in range(6):
        model.add("write" if index % 2 else "read", hold_beats=2)
    model.run()
    assert model.violations == []

    janitor = _Model(_Scheduler(seed=seed + 100, crash_probability=0.0, max_crashes=0))
    janitor.files.files = model.files.files
    # Held long enough for the sweep to see an orphaned record unchanged for a full threshold and collect it.
    janitor.add("write", hold_beats=8)
    janitor.run()
    assert janitor.violations == []
    assert janitor.outcomes[0].granted_at is not None

    remaining = sorted(model.files.files)
    generations = [name for name in remaining if name.startswith(f"{_ROOT}/gen/") and "/.commit-" not in name]
    assert generations, "the log must keep its latest generation"
    assert not [name for name in remaining if name.startswith(f"{_ROOT}/holders/")]
    latest = model.files.files[generations[-1]]
    assert b"writer=" not in latest
    assert b"reader=" not in latest


@pytest.mark.timeout(120)
def test_fencing_generations_are_strictly_increasing_across_grants() -> None:
    scheduler = _Scheduler(seed=7, crash_probability=0.0, max_crashes=0)
    model = _Model(scheduler)
    for index in range(9):
        model.add("write" if index % 3 == 0 else "read", hold_beats=1)
    model.run()
    assert model.violations == []
    writers = sorted(outcome.granted_at or 0 for outcome in model.outcomes if outcome.mode == "write")
    assert len(set(writers)) == len(writers)
    for outcome in model.outcomes:
        assert outcome.granted_at is not None
        if outcome.mode == "read":
            assert outcome.granted_at not in writers
