"""The record formats, the ledger, the generation log, and the storage layer, each on its own."""

from __future__ import annotations

import os
from errno import EIO
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from filelock import SoftFileLockProtocolError
from filelock._soft_rw import _storage as storage_mod
from filelock._soft_rw._protocol import GenerationLog, Ledger, Participant, Snapshot, encode_holder, parse_snapshot
from filelock._soft_rw._storage import OsFiles

if TYPE_CHECKING:
    from pytest_mock import MockerFixture

_FIRST = "0123456789abcdef0123456789abcdef"
_SECOND = "fedcba9876543210fedcba9876543210"

pytestmark = pytest.mark.requires_hard_links


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
    participant = Participant(
        OsFiles(lock_file), lock_file, f"{lock_file}.rw", "read", stale_threshold=1, clock=lambda: 0.0
    )
    participant.publish()
    assert Path(f"{lock_file}.rw", "holders", participant.token).exists()
    participant.leave()
    assert not Path(f"{lock_file}.rw", "holders", participant.token).exists()
    assert participant.generation is None


def test_leave_retries_a_commit_a_peer_won(tmp_path: Path, mocker: MockerFixture) -> None:
    # A peer can publish the next generation between leave() reading the latest one and linking its successor; the
    # holder then re-reads and commits itself out of whatever the peer published.
    lock_file = str(tmp_path / "x.lock")
    files = OsFiles(lock_file)
    root = f"{lock_file}.rw"
    reader = Participant(files, lock_file, root, "read", stale_threshold=100, clock=lambda: 0.0)
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


def test_waiting_contender_republishes_a_swept_record(tmp_path: Path) -> None:
    # A record removed out from under a contender (a sweeper that mistook it, an operator) comes back on its next poll,
    # so the contender is never admitted with nothing for peers to watch.
    lock_file = str(tmp_path / "x.lock")
    files = OsFiles(lock_file)
    root = f"{lock_file}.rw"
    writer = Participant(files, lock_file, root, "write", stale_threshold=100, clock=lambda: 0.0)
    writer.publish()
    assert writer.advance()
    reader = Participant(files, lock_file, root, "read", stale_threshold=100, clock=lambda: 0.0)
    reader.publish()
    record = Path(root, "holders", reader.token)
    record.unlink()
    assert not reader.advance()
    assert record.exists()


def test_waiting_contender_keeps_an_undeletable_record(tmp_path: Path, mocker: MockerFixture) -> None:
    # Windows can leave the record in place while refusing the rewrite; the contender then just carries on.
    lock_file = str(tmp_path / "x.lock")
    files = OsFiles(lock_file)
    root = f"{lock_file}.rw"
    reader = Participant(files, lock_file, root, "read", stale_threshold=100, clock=lambda: 0.0)
    reader.publish()
    mocker.patch.object(OsFiles, "overwrite", return_value=False)
    assert reader.advance()
    assert Path(root, "holders", reader.token).exists()


def test_blocked_contender_still_evicts_stale_readers(tmp_path: Path) -> None:
    # A live writer blocks entry, but a dead reader named beside it is evicted anyway so the writer can drain.
    lock_file = str(tmp_path / "x.lock")
    files = OsFiles(lock_file)
    root = f"{lock_file}.rw"
    clock = [0.0]
    dead = Participant(files, lock_file, root, "read", stale_threshold=1, clock=lambda: clock[0])
    dead.publish()
    assert dead.advance()
    writer = Participant(files, lock_file, root, "write", stale_threshold=1, clock=lambda: clock[0])
    writer.publish()
    assert not writer.advance()
    contender = Participant(files, lock_file, root, "read", stale_threshold=1, clock=lambda: clock[0])
    contender.publish()
    assert not contender.advance()
    clock[0] = 5.0
    assert writer.heartbeat() == "ok"  # keeps its own nonce fresh, so only the dead reader goes stale
    assert not contender.advance()
    latest = GenerationLog(files, lock_file, root).latest()
    assert latest.writer == writer.token
    assert latest.readers == frozenset()


def test_read_missing_file_is_none(tmp_path: Path) -> None:
    assert OsFiles(str(tmp_path / "x.lock")).read(str(tmp_path / "absent")) is None


def test_read_other_errors_propagate(tmp_path: Path, mocker: MockerFixture) -> None:
    mocker.patch.object(storage_mod.os, "open", side_effect=OSError(EIO, "Input/output error"))
    with pytest.raises(OSError, match="Input/output error"):
        OsFiles(str(tmp_path / "x.lock")).read(str(tmp_path / "absent"))


def test_read_non_regular_file_is_empty(tmp_path: Path) -> None:
    # The null device is a character device on every platform, so it stands in for anything planted at a record path
    # that is not a regular file.
    assert OsFiles(str(tmp_path / "x.lock")).read(os.devnull) == b""


def test_create_rolls_back_a_failed_write(tmp_path: Path, mocker: MockerFixture) -> None:
    mocker.patch.object(storage_mod, "write_all", side_effect=OSError("write boom"))
    with pytest.raises(OSError, match="write boom"):
        OsFiles(str(tmp_path / "x.lock")).create(str(tmp_path / "record"), b"data")
    assert not (tmp_path / "record").exists()


def test_link_trusts_identity_over_an_error(tmp_path: Path, mocker: MockerFixture) -> None:
    # An NFS retransmit can report a link that landed as failed; the target naming the source's file is what counts.
    real_link = storage_mod._link_no_follow

    def link_then_fail(source: str, target: str) -> None:
        real_link(source, target)
        raise OSError(EIO, "Input/output error")

    mocker.patch.object(storage_mod, "_link_no_follow", side_effect=link_then_fail)
    source = tmp_path / "source"
    source.write_bytes(b"data")
    assert OsFiles(str(tmp_path / "x.lock")).link(str(source), str(tmp_path / "target"))


def test_link_reports_an_unsupported_filesystem(tmp_path: Path, mocker: MockerFixture) -> None:
    mocker.patch.object(storage_mod, "_link_no_follow", side_effect=NotImplementedError("no os.link"))
    source = tmp_path / "source"
    source.write_bytes(b"data")
    with pytest.raises(SoftFileLockProtocolError, match="hard-link"):
        OsFiles(str(tmp_path / "x.lock")).link(str(source), str(tmp_path / "target"))


def test_link_from_a_missing_source_is_false(tmp_path: Path) -> None:
    assert not OsFiles(str(tmp_path / "x.lock")).link(str(tmp_path / "absent"), str(tmp_path / "target"))


def test_overwrite_missing_file_is_false(tmp_path: Path) -> None:
    assert not OsFiles(str(tmp_path / "x.lock")).overwrite(str(tmp_path / "absent"), b"data")


def test_overwrite_other_errors_propagate(tmp_path: Path, mocker: MockerFixture) -> None:
    mocker.patch.object(storage_mod.os, "open", side_effect=OSError(EIO, "Input/output error"))
    with pytest.raises(OSError, match="Input/output error"):
        OsFiles(str(tmp_path / "x.lock")).overwrite(str(tmp_path / "absent"), b"data")


def test_unlink_tolerates_a_refused_removal(tmp_path: Path, mocker: MockerFixture) -> None:
    mocker.patch.object(Path, "unlink", side_effect=PermissionError("held open"))
    OsFiles(str(tmp_path / "x.lock")).unlink(str(tmp_path / "record"))


def test_unlink_other_errors_propagate(tmp_path: Path, mocker: MockerFixture) -> None:
    mocker.patch.object(Path, "unlink", side_effect=OSError(EIO, "Input/output error"))
    with pytest.raises(OSError, match="Input/output error"):
        OsFiles(str(tmp_path / "x.lock")).unlink(str(tmp_path / "record"))


def test_listdir_missing_directory_is_empty(tmp_path: Path) -> None:
    assert OsFiles(str(tmp_path / "x.lock")).listdir(str(tmp_path / "absent")) == []
