from __future__ import annotations

import subprocess  # ruff:ignore[suspicious-subprocess-import]  # tracing state stays in a child interpreter
import sys
from errno import EIO
from pathlib import Path
from typing import TYPE_CHECKING, Final

import pytest
from capabilities import CAPABILITIES

from filelock import SoftFileLockProtocolError
from filelock._soft._read_write import _generations as protocol_mod
from filelock._soft._read_write import _storage as storage_mod
from filelock._soft._read_write._generations import GenerationLog, Snapshot
from filelock._soft._read_write._participant import Participant
from filelock._soft._read_write._storage import OsFiles, TreeReplacedError
from filelock._soft._read_write._sweep import Ledger

from .conftest import FIRST, SECOND, make_participant

if TYPE_CHECKING:
    from pytest_mock import MockerFixture
from filelock._soft._read_write import _sweep as sweep_mod

pytestmark: Final[pytest.MarkDecorator] = pytest.mark.usefixtures("flushes")


def test_ledger_measures_how_long_a_value_stays_unchanged() -> None:
    clock = [10.0]
    ledger = Ledger(lambda: clock[0])
    assert ledger.observe("a", b"1", asked=clock[0]) == pytest.approx(0.0)
    clock[0] = 12.5
    assert ledger.observe("a", b"1", asked=clock[0]) == pytest.approx(2.5)
    assert ledger.observe("a", b"2", asked=clock[0]) == pytest.approx(0.0)
    clock[0] = 13.0
    assert ledger.observe("a", None, asked=clock[0]) == pytest.approx(0.0)
    clock[0] = 14.0
    assert ledger.observe("a", None, asked=clock[0]) == pytest.approx(1.0)
    ledger.forget("a")
    assert ledger.observe("a", None, asked=clock[0]) == pytest.approx(0.0)


@pytest.mark.skipif(not CAPABILITIES["tracemalloc-allocations"], reason="requires allocation tracing")
def test_ledger_keeps_large_observations_with_bounded_memory() -> None:
    script = (
        "import tracemalloc\n"
        "from filelock._soft._read_write._sweep import Ledger\n"
        "ledger = Ledger(lambda: 0.0)\n"
        "tracemalloc.start()\n"
        "baseline = tracemalloc.get_traced_memory()[0]\n"
        "for number in range(32):\n"
        "    ledger.observe(str(number), bytes((number,)) * (1 << 20), asked=0.0)\n"
        "print(tracemalloc.get_traced_memory()[0] - baseline)\n"
    )
    assert int(subprocess.check_output([sys.executable, "-c", script], text=True)) < 4 << 20


def test_ledger_counts_no_time_past_the_moment_the_observer_asked() -> None:
    clock = [10.0]
    ledger = Ledger(lambda: clock[0])
    assert ledger.observe("a", b"1", asked=4.0) == pytest.approx(0.0)
    clock[0] = 50.0
    assert ledger.observe("a", b"1", asked=12.5) == pytest.approx(2.5)


def test_ledger_retain_forgets_everything_else() -> None:
    clock = [0.0]
    ledger = Ledger(lambda: clock[0])
    ledger.observe("kept", b"1", asked=clock[0])
    ledger.observe("gone", b"1", asked=clock[0])
    clock[0] = 5.0
    ledger.retain({"kept"})
    assert ledger.observe("kept", b"1", asked=clock[0]) == pytest.approx(5.0)
    assert ledger.observe("gone", b"1", asked=clock[0]) == pytest.approx(0.0)


@pytest.mark.requires_hard_links
def test_an_overfull_generation_list_collects_temporary_files(
    lock_file: str, files: OsFiles, root: str, mocker: MockerFixture
) -> None:
    files.prepare(root)
    for module in (protocol_mod, storage_mod):
        mocker.patch.object(module, "MAX_LISTED_NAMES", 3)
    for number in range(3):
        Path(root, "gen", f".commit-{number:032x}").touch()
    clock = [0.0]
    reader = make_participant(files, lock_file, "read", stale_threshold=10, clock=lambda: clock[0])
    reader.publish()
    with pytest.raises(SoftFileLockProtocolError, match="holds more than 3 names"):
        reader.advance()
    clock[0] = 10.0
    with pytest.raises(SoftFileLockProtocolError, match="holds more than 3 names"):
        reader.advance()
    assert not list(Path(root, "gen").glob(".commit-*"))
    assert reader.advance()


@pytest.mark.requires_hard_links
def test_commit_keeps_a_published_generation_when_listing_overflows(
    lock_file: str, files: OsFiles, root: str, mocker: MockerFixture
) -> None:
    files.prepare(root)
    log = GenerationLog(files, lock_file, root)
    assert log.latest().generation == 0
    mocker.patch.object(protocol_mod, "MAX_LISTED_NAMES", 1)
    snapshot = Snapshot(generation=1, writer=FIRST, readers=frozenset())

    assert not log.commit(snapshot)
    assert Path(root, "gen", f"{1:020d}").read_bytes() == snapshot.encode()


@pytest.mark.requires_hard_links
def test_heartbeat_collects_temporary_files_when_the_generation_list_is_overfull(
    lock_file: str, files: OsFiles, root: str, mocker: MockerFixture
) -> None:
    clock = [0.0]
    reader = make_participant(files, lock_file, "read", stale_threshold=10, clock=lambda: clock[0])
    reader.publish()
    assert reader.advance()
    for module in (protocol_mod, storage_mod):
        mocker.patch.object(module, "MAX_LISTED_NAMES", 3)
    temporary = Path(root, "gen", f".commit-{FIRST}")
    temporary.touch()
    clock[0] = 5.0
    assert reader.heartbeat() == ("ok", None)
    clock[0] = 15.0
    assert reader.heartbeat() == ("ok", None)
    assert not temporary.exists()


@pytest.mark.requires_hard_links
@pytest.mark.parametrize(
    "error",
    [
        pytest.param(OSError(EIO, "read failed"), id="io-error"),
        pytest.param(TreeReplacedError("tree replaced"), id="tree-replaced"),
    ],
)
def test_heartbeat_ignores_a_sweep_error_after_generation_list_overflow(
    lock_file: str, files: OsFiles, root: str, mocker: MockerFixture, error: OSError | TreeReplacedError
) -> None:
    clock = [0.0]
    reader = make_participant(files, lock_file, "read", stale_threshold=10, clock=lambda: clock[0])
    reader.publish()
    assert reader.advance()
    for module in (protocol_mod, storage_mod):
        mocker.patch.object(module, "MAX_LISTED_NAMES", 3)
    Path(root, "gen", f".commit-{FIRST}").touch()
    holder = Path(root, "holders", reader.token)
    record = holder.read_bytes()
    clock[0] = 5.0
    mocker.patch.object(OsFiles, "listdir", autospec=True, side_effect=[files.listdir(str(Path(root, "gen"))), error])
    assert reader.heartbeat() == ("ok", None)
    assert holder.read_bytes() != record


def test_overfull_generation_sweep_forgets_missing_temporary_files(
    lock_file: str, files: OsFiles, root: str, mocker: MockerFixture
) -> None:
    files.prepare(root)
    for module in (protocol_mod, storage_mod):
        mocker.patch.object(module, "MAX_LISTED_NAMES", 2)
    clock = [0.0]
    ledger = Ledger(lambda: clock[0])
    reader = Participant(
        files,
        root,
        "read",
        stale_threshold=10,
        clock=lambda: clock[0],
        lease_clock=lambda: clock[0],
        ledger=ledger,
        log=GenerationLog(files, lock_file, root),
    )
    reader.publish()
    first = Path(root, "gen", f".commit-{FIRST}")
    second = Path(root, "gen", f".commit-{SECOND}")
    first.touch()
    second.touch()
    ledger.observe(FIRST, b"member", asked=clock[0])
    with pytest.raises(SoftFileLockProtocolError, match="holds more than 2 names"):
        reader.advance()
    first.unlink()
    Path(root, "gen", f".commit-{reader.token}").touch()
    clock[0] = 5.0
    with pytest.raises(SoftFileLockProtocolError, match="holds more than 2 names"):
        reader.advance()
    assert (
        ledger.observe(f"temporary:{first.name}", b"", asked=clock[0]),
        ledger.observe(f"temporary:{second.name}", b"", asked=clock[0]),
        ledger.observe(FIRST, b"member", asked=clock[0]),
    ) == (0.0, 5.0, 5.0)


@pytest.mark.requires_hard_links
def test_a_tighter_peer_waits_out_the_holders_own_lease(lock_file: str, files: OsFiles) -> None:
    # Use the holder's lease when peers have different heartbeat intervals.
    clock = [0.0]
    holder = make_participant(files, lock_file, "write", stale_threshold=10, clock=lambda: clock[0])
    holder.publish()
    assert holder.advance()
    peer = make_participant(files, lock_file, "write", stale_threshold=1, clock=lambda: clock[0])
    peer.publish()
    assert not peer.advance()
    clock[0] = 5.0
    assert not peer.advance()
    clock[0] = 11.0
    assert peer.advance()


@pytest.mark.requires_hard_links
def test_a_contender_suspended_after_its_read_does_not_evict_a_live_holder(
    lock_file: str, files: OsFiles, root: str, mocker: MockerFixture
) -> None:
    # A pause after the read must not age its observation.
    clock = [0.0]
    holder = make_participant(files, lock_file, "write", stale_threshold=10, clock=lambda: clock[0])
    holder.publish()
    assert holder.advance()
    peer = make_participant(files, lock_file, "write", stale_threshold=10, clock=lambda: clock[0])
    peer.publish()
    assert not peer.advance()
    record, real_read = str(Path(root, "holders", holder.token)), files.read

    def read_then_sleep_through_a_heartbeat(path: str) -> bytes | None:
        data = real_read(path)
        if path == record:
            clock[0] += 11.0
            assert holder.heartbeat() == ("ok", None)
        return data

    mocker.patch.object(files, "read", autospec=True, side_effect=read_then_sleep_through_a_heartbeat)
    assert not peer.advance()
    assert holder.heartbeat() == ("ok", None)


@pytest.mark.requires_hard_links
def test_the_sweep_forgets_peers_that_left(lock_file: str, files: OsFiles, root: str) -> None:
    clock = [0.0]
    ledger = Ledger(lambda: clock[0])

    def waiter() -> Participant:
        log = GenerationLog(files, lock_file, root)
        return Participant(
            files,
            root,
            "read",
            stale_threshold=10,
            clock=lambda: clock[0],
            lease_clock=lambda: clock[0],
            ledger=ledger,
            log=log,
        )

    writer = make_participant(files, lock_file, "write", stale_threshold=10, clock=lambda: clock[0])
    writer.publish()
    assert writer.advance()
    record = Path(root, "holders", writer.token).read_bytes()
    (first := waiter()).publish()
    assert not first.advance()
    first.leave()
    writer.leave()
    clock[0] = 6.0
    (second := waiter()).publish()
    assert second.advance()
    second.leave()
    assert ledger.observe(writer.token, record, asked=clock[0]) == pytest.approx(0.0)


@pytest.mark.requires_hard_links
def test_the_sweep_collects_orphans_a_batch_at_a_time(
    lock_file: str, files: OsFiles, root: str, mocker: MockerFixture
) -> None:
    mocker.patch.object(sweep_mod, "HOUSEKEEPING_BATCH", 2)
    clock = [0.0]
    real_listdir = OsFiles.listdir
    mocker.patch.object(
        OsFiles,
        "listdir",
        autospec=True,
        side_effect=lambda self, path: sorted(real_listdir(self, path), reverse=bool(int(clock[0] / 10) % 2)),
    )
    files.prepare(root)
    for number in range(5):
        Path(root, "holders", f"{number:032x}").touch()
        Path(root, "gen", f".commit-{number:032x}").touch()
    ledger = Ledger(lambda: clock[0])
    left: list[tuple[int, int]] = []
    for clock[0] in (0.0, 10.0, 20.0, 30.0, 40.0, 50.0):
        reader = Participant(
            files,
            root,
            "read",
            stale_threshold=10,
            clock=lambda: clock[0],
            lease_clock=lambda: clock[0],
            ledger=ledger,
            log=GenerationLog(files, lock_file, root),
        )
        reader.publish()
        assert reader.advance()
        reader.leave()
        left.append((len(list(Path(root, "holders").iterdir())), len(list(Path(root, "gen").glob(".commit-*")))))
    assert left == [(5, 5), (5, 5), (5, 5), (3, 3), (1, 1), (0, 0)]
