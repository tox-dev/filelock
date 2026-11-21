from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Final

import pytest

from filelock import SoftFileLockProtocolError
from filelock._soft._read_write._generations import (
    _RETAINED_GENERATIONS,
    GenerationLog,
    Snapshot,
    parse_snapshot,
)
from filelock._soft._read_write._storage import OsFiles

from .conftest import FIRST, SECOND, committed

if TYPE_CHECKING:
    from pytest_mock import MockerFixture

pytestmark: Final[pytest.MarkDecorator] = pytest.mark.usefixtures("flushes")


@pytest.mark.parametrize(
    "snapshot",
    [
        pytest.param(Snapshot(generation=0, writer=None, readers=frozenset()), id="empty"),
        pytest.param(Snapshot(generation=7, writer=FIRST, readers=frozenset()), id="writer"),
        pytest.param(Snapshot(generation=12, writer=None, readers=frozenset({FIRST, SECOND})), id="readers"),
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
        pytest.param(f"filelock-rw/1\ngeneration=1\nwriter={FIRST}\nwriter={SECOND}\n".encode(), id="two-writers"),
        pytest.param(f"filelock-rw/1\ngeneration=1\nreader={FIRST}\nreader={FIRST}\n".encode(), id="reader-twice"),
        pytest.param(b"filelock-rw/1\ngeneration=1\nwriter=short\n", id="bad-token"),
        pytest.param(b"filelock-rw/1\ngeneration=1\ncolor=blue\n", id="unknown-key"),
        pytest.param(b"filelock-rw/1\ngeneration=1\nno separator\n", id="no-separator"),
    ],
)
def test_malformed_snapshots_parse_to_none(data: bytes) -> None:
    assert parse_snapshot(data) is None


def test_log_starts_empty(lock_file: str, files: OsFiles, root: str) -> None:
    log = GenerationLog(files, lock_file, root)
    assert log.latest() == Snapshot(generation=0, writer=None, readers=frozenset())


def test_a_commit_into_a_log_without_its_directory_is_lost(lock_file: str, files: OsFiles, root: str) -> None:
    assert not GenerationLog(files, lock_file, root).commit(Snapshot(generation=1, writer=FIRST, readers=frozenset()))


@pytest.mark.requires_hard_links
def test_an_epoch_a_peer_created_first_is_adopted(
    lock_file: str, files: OsFiles, root: str, mocker: MockerFixture
) -> None:
    # A peer can create the epoch after the read.
    files.prepare(root)
    epoch = Path(root, "gen", "epoch")
    epoch.write_bytes(b"peer\n")
    real_read = OsFiles.read
    missed = []

    def miss_the_epoch_once(self: OsFiles, path: str) -> bytes | None:
        if path == str(epoch) and not missed:
            missed.append(path)
            return None
        return real_read(self, path)

    mocker.patch.object(OsFiles, "read", autospec=True, side_effect=miss_the_epoch_once)
    log = GenerationLog(files, lock_file, root)
    assert log.latest() == Snapshot(generation=0, writer=None, readers=frozenset())
    assert missed
    assert epoch.read_bytes() == b"peer\n"
    assert log.commit(Snapshot(generation=1, writer=FIRST, readers=frozenset()))


def test_an_empty_log_reads_as_empty_again(lock_file: str, files: OsFiles, root: str) -> None:
    # An empty log has no gen/0.
    files.prepare(root)
    log = GenerationLog(files, lock_file, root)
    assert log.latest() == log.latest() == Snapshot(generation=0, writer=None, readers=frozenset())


@pytest.mark.requires_hard_links
def test_commit_refuses_a_generation_that_exists(lock_file: str, files: OsFiles, root: str) -> None:
    files.prepare(root)
    first = GenerationLog(files, lock_file, root)
    second = GenerationLog(files, lock_file, root)
    assert first.latest().generation == 0
    assert second.latest().generation == 0
    assert first.commit(Snapshot(generation=1, writer=FIRST, readers=frozenset()))
    assert not second.commit(Snapshot(generation=1, writer=SECOND, readers=frozenset()))
    assert second.latest().writer == FIRST
    assert sorted(entry.name for entry in Path(root, "gen").iterdir() if entry.name.isdigit()) == [f"{1:020d}"]


@pytest.mark.requires_hard_links
def test_rescan_skips_a_generation_removed_after_listing(
    lock_file: str, files: OsFiles, root: str, mocker: MockerFixture
) -> None:
    committed(files, lock_file, root, [None, FIRST])
    # Older releases wrote logs without HEAD.
    Path(root, "gen", "HEAD").unlink()
    real_read = OsFiles.read
    latest_path = str(Path(root, "gen", f"{2:020d}"))
    mocker.patch.object(OsFiles, "read", lambda self, path: None if path == latest_path else real_read(self, path))
    fresh = GenerationLog(files, lock_file, root)
    assert fresh.latest().generation == 1


def test_malformed_generation_raises(lock_file: str, files: OsFiles, root: str) -> None:
    files.prepare(root)
    Path(root, "gen", f"{1:020d}").write_bytes(b"filelock-rw/1\ngeneration=5\n")
    log = GenerationLog(files, lock_file, root)
    with pytest.raises(SoftFileLockProtocolError, match="malformed generation record") as caught:
        log.latest()
    assert caught.value.claim_name == f"{1:020d}"


def test_non_ascii_digit_generation_name_is_ignored(lock_file: str, files: OsFiles, root: str) -> None:
    files.prepare(root)
    Path(root, "gen", f"{1:020d}").write_bytes(Snapshot(generation=1, writer=FIRST, readers=frozenset()).encode())
    Path(root, "gen", "²" * 20).write_bytes(b"not a generation")
    assert GenerationLog(files, lock_file, root).latest() == Snapshot(generation=1, writer=FIRST, readers=frozenset())


@pytest.mark.requires_hard_links
def test_latest_probes_across_a_compaction_hole(lock_file: str, files: OsFiles, root: str) -> None:
    files.prepare(root)
    log = GenerationLog(files, lock_file, root)
    for generation in range(1, 6):
        assert log.commit(Snapshot(generation=generation, writer=None, readers=frozenset()))
    Path(root, "gen", f"{3:020d}").unlink()
    behind = GenerationLog(files, lock_file, root)
    assert behind.latest().generation == 5


def test_probe_stops_following_continuous_growth(
    lock_file: str, files: OsFiles, root: str, mocker: MockerFixture
) -> None:
    log = GenerationLog(files, lock_file, root)
    reads = []

    def read_next(generation: int) -> Snapshot:
        reads.append(generation)
        return Snapshot(generation=generation, writer=None, readers=frozenset())

    mocker.patch.object(log, "_read", autospec=True, side_effect=read_next)
    latest = log._probe(Snapshot(generation=0, writer=None, readers=frozenset()))

    assert latest.generation == 2 * _RETAINED_GENERATIONS
    assert len(reads) == latest.generation


def test_a_listing_naming_only_compacted_generations_fails_closed(
    lock_file: str, files: OsFiles, root: str, mocker: MockerFixture
) -> None:
    # Treating a stale listing as empty would fork the generation sequence.
    files.prepare(root)
    mocker.patch.object(OsFiles, "listdir", return_value=[f"{generation:020d}" for generation in (4, 5)], autospec=True)
    with pytest.raises(SoftFileLockProtocolError, match="names no readable snapshot"):
        GenerationLog(files, lock_file, root).latest()


@pytest.mark.requires_hard_links
def test_a_listing_compacted_from_under_the_reader_is_taken_again(
    lock_file: str, files: OsFiles, root: str, mocker: MockerFixture
) -> None:
    # Compaction can remove every generation in the first listing.
    files.prepare(root)
    log = GenerationLog(files, lock_file, root)
    assert log.commit(Snapshot(generation=1, writer=FIRST, readers=frozenset()))
    real_listdir = OsFiles.listdir
    listings = iter([[f"{generation:020d}" for generation in (4, 5)]])
    mocker.patch.object(OsFiles, "listdir", lambda self, path: next(listings, None) or real_listdir(self, path))
    assert GenerationLog(files, lock_file, root).latest().writer == FIRST
