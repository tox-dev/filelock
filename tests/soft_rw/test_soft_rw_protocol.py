"""
Model check of the generation-log protocol under an adversarial scheduler.

Every participant runs on its own thread over an in-memory filesystem, and every filesystem call hands the turn back to
a seeded scheduler that picks which participant runs next, advances a fake clock, and sometimes kills the participant
it just picked. That interleaves the protocol at the granularity of single filesystem operations, including inside a
commit, and a crash at any of those points leaves exactly what a dead process would leave on disk.
"""

from __future__ import annotations

import random
import threading
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Final, Literal

import pytest

from filelock._soft_rw._protocol import GenerationLog, Ledger, Participant

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
