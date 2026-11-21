from __future__ import annotations

import random
import threading
from itertools import cycle, islice
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Final, Literal

import pytest

from filelock._core import override
from filelock._soft._read_write._generations import GenerationLog, Snapshot
from filelock._soft._read_write._participant import Participant
from filelock._soft._read_write._storage import RecordFiles
from filelock._soft._read_write._sweep import Ledger

from .conftest import SECOND

if TYPE_CHECKING:
    from collections.abc import Iterator

    from pytest_mock import MockerFixture

pytestmark: Final[pytest.MarkDecorator] = pytest.mark.usefixtures("flushes")


# Each simulated filesystem operation takes less than a heartbeat interval.
_STEP: Final[float] = 0.001


# Only crashed participants should outlive this delay.
_FAIRNESS: Final[float] = 8 * _STEP


_ROOT: Final[str] = "/lock.rw"


@pytest.mark.parametrize(
    ("seed", "crash_probability", "stale_listings"),
    [
        pytest.param(
            seed,
            crashes,
            stale,
            id=f"seed{seed}-{'crashes' if crashes else 'clean'}-{'stale-listings' if stale else 'fresh-listings'}",
        )
        for seed, crashes, stale in (
            (number, 0.02 if number % 3 else 0.0, 0.3 if number % 2 else 0.0) for number in range(24)
        )
    ],
)
@pytest.mark.timeout(120)
def test_model_never_overlaps_and_always_progresses(seed: int, crash_probability: float, stale_listings: float) -> None:
    scheduler = _Scheduler(seed=seed, crash_probability=crash_probability, max_crashes=3)
    model = _Model(scheduler, stale_listings=stale_listings)
    rng = random.Random(seed)  # ruff: ignore[suspicious-non-cryptographic-random-usage]  # a seeded mix of hold lengths, not entropy
    modes: Iterator[Literal["read", "write"]] = cycle(("write", "read", "read"))
    for mode in islice(modes, 8):
        model.add(mode, hold_beats=rng.randint(1, 4))
    model.run()

    assert model.violations == []
    survivors = [outcome for outcome in model.outcomes if not outcome.crashed]
    assert [outcome for outcome in survivors if outcome.stuck] == []
    assert [outcome for outcome in survivors if outcome.lost] == []
    assert all(outcome.granted_at is not None for outcome in survivors)
    assert scheduler.crashes == sum(outcome.crashed for outcome in model.outcomes)


class _Scheduler:
    # Bound the time between turns so a runnable participant never appears stale.
    __slots__ = (
        "_condition",
        "_crashed",
        "_expected",
        "_live",
        "_parked",
        "_random",
        "_turn",
        "clock",
        "crash_probability",
        "crashes",
        "max_crashes",
        "seed",
    )

    def __init__(self, *, seed: int, crash_probability: float, max_crashes: int) -> None:
        self.seed = seed
        self.crash_probability = crash_probability
        self.max_crashes = max_crashes
        self.clock = 0.0
        self.crashes = 0
        self._random = random.Random(seed)  # ruff: ignore[suspicious-non-cryptographic-random-usage]  # a seeded schedule is the point, not entropy
        self._condition = threading.Condition()
        self._expected = 0
        self._live: set[int] = set()
        self._parked: dict[int, float] = {}
        self._turn: int | None = None
        self._crashed: set[int] = set()

    def expect(self) -> None:
        # The first step must see the worker before its thread runs.
        with self._condition:
            self._expected += 1

    def register(self) -> None:
        with self._condition:
            self._expected -= 1
            self._live.add(threading.get_ident())
            self._condition.notify_all()

    def sleep(self, seconds: float) -> None:
        self.yield_turn(wake_at=self.clock + seconds)

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

    def finish(self) -> None:
        with self._condition:
            self._live.discard(threading.get_ident())
            self._parked.pop(threading.get_ident(), None)
            self._condition.notify_all()

    def step(self) -> bool:
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


class _Crashed(BaseException): ...


class _Model:
    def __init__(self, scheduler: _Scheduler, *, stale_listings: float = 0.0) -> None:
        self.scheduler = scheduler
        self.files = _MemoryFiles(scheduler, stale_listings=stale_listings)
        self.holding: dict[str, Literal["read", "write"]] = {}
        self.last_granted = 0
        self.last_writer = 0
        self.outcomes: list[_Outcome] = []
        self.threads: list[threading.Thread] = []
        self.violations: list[str] = []

    def add(self, mode: Literal["read", "write"], hold_beats: int) -> None:
        outcome = _Outcome(mode=mode)
        self.outcomes.append(outcome)
        self.scheduler.expect()
        self.threads.append(threading.Thread(target=self._run, args=(outcome, hold_beats), daemon=True))

    def _run(self, outcome: _Outcome, hold_beats: int) -> None:
        self.scheduler.register()
        participant = Participant(
            self.files,
            _ROOT,
            outcome.mode,
            stale_threshold=1.0,
            clock=lambda: self.scheduler.clock,
            lease_clock=lambda: self.scheduler.clock,
            ledger=Ledger(lambda: self.scheduler.clock),
            log=GenerationLog(self.files, "/lock", _ROOT),
        )
        poll: float = 0.1
        try:
            participant.publish()
            deadline = self.scheduler.clock + 120.0
            while not participant.advance():
                if (
                    self.scheduler.clock >= deadline
                ):  # pragma: no cover  # a liveness regression, reported by the assertion
                    outcome.stuck = True
                    return
                self.scheduler.sleep(poll)
            outcome.granted_at = participant.generation
            self._enter(participant.token, outcome.mode, outcome.granted_at)
            heartbeat: float = 0.3
            for _ in range(hold_beats):
                self.scheduler.sleep(heartbeat)
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
        # The participant owns the scheduler turn throughout this check.
        writers = [held for held in self.holding.values() if held == "write"]
        # Concurrent readers can enter out of commit order.
        floor = self.last_granted if mode == "write" else self.last_writer
        if granted_at is None or granted_at <= floor:  # pragma: no cover  # a forked log, reported by the assertion
            self.violations.append(f"{mode} granted at generation {granted_at} after generation {floor}")
        else:
            self.last_granted = max(self.last_granted, granted_at)
            if mode == "write":
                self.last_writer = granted_at
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


class _MemoryFiles(RecordFiles):
    def __init__(self, scheduler: _Scheduler, *, stale_listings: float = 0.0) -> None:
        self._scheduler = scheduler
        self.files: dict[str, bytes] = {}
        # Older listings model a client with a stale directory cache.
        self._stale_listings = stale_listings
        self._random = random.Random(scheduler.seed + 1)  # ruff: ignore[suspicious-non-cryptographic-random-usage]  # a seeded schedule, not entropy
        self._history: list[list[str]] = [[]]

    @override
    def read(self, path: str) -> bytes | None:
        self._scheduler.yield_turn()
        return self.files.get(_key(path))

    @override
    def link(self, source: str, target: str) -> bool:
        self._scheduler.yield_turn()
        if _key(target) in self.files or _key(source) not in self.files:
            return False
        self.files[_key(target)] = self.files[_key(source)]
        self._record()
        return True

    def _record(self) -> None:
        self._history.append(sorted(self.files))
        del self._history[:-128]

    @override
    def write_head(self, path: str, temporary: str, data: bytes) -> None:
        stage = str(Path(path).with_name(temporary))
        self.create(stage, data)
        self._scheduler.yield_turn()
        self.files[_key(path)] = self.files.pop(_key(stage))
        self._record()

    @override
    def create(self, path: str, data: bytes, *, durable: bool = False) -> tuple[int, int]:  # ruff:ignore[unused-method-argument]  # memory has no storage to flush
        self._scheduler.yield_turn()
        if _key(path) in self.files:  # pragma: no cover  # every name created here is a fresh token
            raise FileExistsError(path)
        self.files[_key(path)] = data
        self._record()
        return 0, 0

    @override
    def overwrite(self, path: str, data: bytes, identity: tuple[int, int], header: bytes) -> bool:  # ruff:ignore[unused-method-argument]  # nothing here swaps one file for another
        self._scheduler.yield_turn()
        if (
            _key(path) not in self.files
        ):  # pragma: no cover  # only a crashed participant loses its record, and it never writes again
            return False
        self.files[_key(path)] = data
        self._record()
        return True

    @override
    def unlink(self, path: str, *, witness: str | None = None) -> None:
        self._scheduler.yield_turn()
        if witness is None or _key(witness) in self.files:
            self.files.pop(_key(path), None)
            self._record()

    @override
    def listdir(self, path: str) -> list[str]:
        self._scheduler.yield_turn()
        names = (
            self._random.choice(self._history) if self._random.random() < self._stale_listings else sorted(self.files)
        )
        return [PurePosixPath(name).name for name in names if str(PurePosixPath(name).parent) == _key(path)]

    @override
    def prepare(self, root: str, *, force: bool = False) -> None:  # ruff:ignore[unused-method-argument]  # nothing to create in memory
        self._scheduler.yield_turn()


def _key(path: str) -> str:
    # Normalize Windows paths for the model filesystem.
    return path.replace("\\", "/")


class _Outcome:
    __slots__ = ("crashed", "granted_at", "lost", "mode", "stuck")

    def __init__(self, *, mode: Literal["read", "write"]) -> None:
        self.mode = mode
        self.granted_at: int | None = None
        self.crashed = False
        self.lost = False
        self.stuck = False


@pytest.mark.parametrize("seed", range(6))
@pytest.mark.timeout(120)
def test_model_janitor_clears_every_crashed_holder(seed: int) -> None:
    model = _Model(_Scheduler(seed=seed, crash_probability=0.05, max_crashes=4))
    modes: Iterator[Literal["read", "write"]] = cycle(("read", "write"))
    for mode in islice(modes, 6):
        model.add(mode, hold_beats=2)
    model.run()
    assert model.violations == []

    janitor = _Model(_Scheduler(seed=seed + 100, crash_probability=0.0, max_crashes=0))
    janitor.files.files = model.files.files
    # Orphan collection requires one full threshold of observations.
    janitor.add("write", hold_beats=8)
    janitor.run()
    assert janitor.violations == []
    assert janitor.outcomes[0].granted_at is not None

    remaining = sorted(model.files.files)
    generations = [name for name in remaining if name.startswith(f"{_ROOT}/gen/") and name.rpartition("/")[2].isdigit()]
    assert generations, "the log must keep its latest generation"
    assert not [name for name in remaining if name.startswith(f"{_ROOT}/holders/")]
    latest = model.files.files[generations[-1]]
    assert b"writer=" not in latest
    assert b"reader=" not in latest


@pytest.mark.timeout(120)
def test_fencing_generations_are_strictly_increasing_across_grants() -> None:
    model = _Model(_Scheduler(seed=7, crash_probability=0.0, max_crashes=0))
    modes: Iterator[Literal["read", "write"]] = cycle(("write", "read", "read"))
    for mode in islice(modes, 9):
        model.add(mode, hold_beats=1)
    model.run()
    assert model.violations == []
    writers = sorted(outcome.granted_at or 0 for outcome in model.outcomes if outcome.mode == "write")
    assert len(set(writers)) == len(writers)
    assert all(outcome.granted_at is not None for outcome in model.outcomes)
    assert [outcome for outcome in model.outcomes if outcome.mode == "read" and outcome.granted_at in writers] == []


def test_a_stale_listing_cannot_prune_the_only_generation_of_a_rebuilt_log(mocker: MockerFixture) -> None:
    files = _MemoryFiles(_Scheduler(seed=7, crash_probability=0, max_crashes=0))
    mocker.patch.object(_Scheduler, "yield_turn", autospec=True)
    log = GenerationLog(files, "model.lock", _ROOT)
    assert log.latest().generation == 0
    writer = Snapshot(generation=1, writer=SECOND, readers=frozenset())
    assert log.commit(writer)
    mocker.patch.object(files, "listdir", autospec=True, return_value=[f"{n:020d}" for n in range(1, 71)])
    assert log.latest() == writer
