from __future__ import annotations

import shutil
from errno import EIO
from pathlib import Path
from typing import TYPE_CHECKING, Final

import pytest

from filelock._soft._read_write._generations import GenerationLog, Snapshot, parse_snapshot
from filelock._soft._read_write._storage import OsFiles

from .conftest import FIRST, SECOND, committed

if TYPE_CHECKING:
    from pytest_mock import MockerFixture

pytestmark: Final[pytest.MarkDecorator] = pytest.mark.usefixtures("flushes")


@pytest.mark.requires_hard_links
@pytest.mark.parametrize("generation", [pytest.param(2, id="slot-absent"), pytest.param(3, id="slot-reused")])
def test_a_commit_overtaken_by_a_wipe_leaves_the_replacement_generation_alone(
    lock_file: str, files: OsFiles, root: str, head: Path, mocker: MockerFixture, generation: int
) -> None:
    old = committed(files, lock_file, root, [None, FIRST])
    real_link = files.link

    def link_then_wipe(source: str, target: str) -> bool:
        landed = real_link(source, target)
        shutil.rmtree(root)
        committed(OsFiles(lock_file), lock_file, root, [None] * (generation - 1) + [SECOND])
        return landed

    mocker.patch.object(files, "link", autospec=True, side_effect=link_then_wipe)
    assert not old.commit(Snapshot(generation=3, writer=None, readers=frozenset()))
    replacement = Snapshot(generation=generation, writer=SECOND, readers=frozenset())
    assert (old.latest(), _published(head)) == (replacement, replacement)


def _published(head: Path) -> Snapshot | None:
    return parse_snapshot(head.read_bytes().partition(b"\n")[2])


@pytest.mark.requires_hard_links
def test_a_commit_that_lands_in_a_compacted_slot_counts_as_lost(
    lock_file: str, files: OsFiles, root: str, mocker: MockerFixture
) -> None:
    # The predecessor can disappear between validation and publication.
    log = committed(files, lock_file, root, [None] * 3)
    real_link = files.link

    def peers_compact_then_link(source: str, target: str) -> bool:
        committed(OsFiles(lock_file), lock_file, root, [None] * 70)
        return real_link(source, target)

    mocker.patch.object(files, "link", autospec=True, side_effect=peers_compact_then_link)
    assert (
        log.commit(Snapshot(generation=4, writer=FIRST, readers=frozenset())),
        Path(root, "gen", f"{4:020d}").exists(),
    ) == (False, False)


@pytest.mark.requires_hard_links
def test_compaction_keeps_a_replacement_writer_at_the_same_generation(
    lock_file: str, files: OsFiles, root: str, mocker: MockerFixture
) -> None:
    log = committed(files, lock_file, root, [None] * 3)
    link, unlink = files.link, files.unlink

    def advance_then_link(source: str, target: str) -> bool:
        committed(OsFiles(lock_file), lock_file, root, [None] * 70)
        return link(source, target)

    def reset_before_compaction(path: str, *, witness: str | None = None) -> None:
        if witness is not None:
            Path(root).rename(f"{root}.old")
            committed(OsFiles(lock_file), lock_file, root, [None] * 3 + [SECOND])
        unlink(path, witness=witness)

    mocker.patch.object(files, "link", autospec=True, side_effect=advance_then_link)
    mocker.patch.object(files, "unlink", autospec=True, side_effect=reset_before_compaction)
    assert not log.commit(Snapshot(generation=4, writer=None, readers=frozenset({FIRST})))
    assert log.latest() == Snapshot(generation=4, writer=SECOND, readers=frozenset())


@pytest.mark.requires_hard_links
@pytest.mark.parametrize("witness", ["listing", "head"])
def test_a_commit_beside_another_late_record_counts_as_lost(
    lock_file: str, files: OsFiles, root: str, head: Path, mocker: MockerFixture, witness: str
) -> None:
    # A delayed committer can restore gen/3 after compaction. Its presence alone cannot justify gen/4.
    log = committed(files, lock_file, root, [None] * 3)
    stale_head, stale_listing, real_link = head.read_bytes(), files.listdir(str(head.parent)), files.link

    def peers_compact_then_both_link(source: str, target: str) -> bool:
        committed(OsFiles(lock_file), lock_file, root, [None] * 70)
        Path(root, "gen", f"{3:020d}").write_bytes(Snapshot(generation=3, writer=None, readers=frozenset()).encode())
        if witness == "listing":
            head.write_bytes(stale_head)
        else:
            mocker.patch.object(OsFiles, "listdir", autospec=True, return_value=stale_listing)
        return real_link(source, target)

    mocker.patch.object(files, "link", autospec=True, side_effect=peers_compact_then_both_link)
    assert (
        log.commit(Snapshot(generation=4, writer=FIRST, readers=frozenset())),
        Path(root, "gen", f"{4:020d}").exists(),
    ) == (False, False)


@pytest.mark.requires_hard_links
@pytest.mark.parametrize("generation", [pytest.param(1, id="first"), pytest.param(3, id="later")])
def test_a_record_refused_inside_the_window_keeps_its_slot(
    lock_file: str, files: OsFiles, root: str, mocker: MockerFixture, generation: int
) -> None:
    # Freeing a slot inside the retained window would let a delayed committer reuse it.
    log = committed(files, lock_file, root, [None] * (generation - 1))
    log.latest()
    real_link = files.link

    def link_then_peers_commit(source: str, target: str) -> bool:
        landed = real_link(source, target)
        committed(OsFiles(lock_file), lock_file, root, [None])
        Path(root, "gen", f"{generation - 1:020d}").unlink(missing_ok=True)
        return landed

    mocker.patch.object(files, "link", autospec=True, side_effect=link_then_peers_commit)
    assert (
        log.commit(Snapshot(generation=generation, writer=FIRST, readers=frozenset())),
        Path(root, "gen", f"{generation:020d}").exists(),
    ) == (False, True)


@pytest.mark.requires_hard_links
def test_a_cold_start_record_keeps_its_slot_when_a_peer_extends_it(
    lock_file: str, files: OsFiles, root: str, mocker: MockerFixture
) -> None:
    files.prepare(root)
    first = GenerationLog(files, lock_file, root)
    assert first.latest().generation == 0
    first_path = Path(root, "gen", f"{1:020d}")
    real_read = files.read
    head_reads_after_link = 0

    def extend_between_checks(path: str) -> bytes | None:
        nonlocal head_reads_after_link
        if path == str(Path(root, "gen", "HEAD")) and first_path.exists():
            head_reads_after_link += 1
            if head_reads_after_link == 2:
                peer = GenerationLog(OsFiles(lock_file), lock_file, root)
                assert peer.latest().generation == 1
                assert peer.commit(Snapshot(generation=2, writer=None, readers=frozenset({FIRST, SECOND})))
        return real_read(path)

    mocker.patch.object(files, "read", autospec=True, side_effect=extend_between_checks)
    assert not first.commit(Snapshot(generation=1, writer=None, readers=frozenset({FIRST})))
    assert first_path.exists()
    assert GenerationLog(OsFiles(lock_file), lock_file, root).latest() == Snapshot(
        generation=2, writer=None, readers=frozenset({FIRST, SECOND})
    )


@pytest.mark.requires_hard_links
def test_a_cold_start_commit_rechecks_the_initialized_epoch(
    lock_file: str, files: OsFiles, root: str, mocker: MockerFixture
) -> None:
    files.prepare(root)
    epoch = Path(root, "gen", "epoch")
    epoch.write_bytes(b"")
    log = GenerationLog(files, lock_file, root)
    assert log.latest().generation == 0
    link = files.link

    def finish_epoch_after_link(source: str, target: str) -> bool:
        landed = link(source, target)
        epoch.write_bytes(f"{SECOND}\n".encode())
        return landed

    mocker.patch.object(files, "link", autospec=True, side_effect=finish_epoch_after_link)
    reader = Snapshot(generation=1, writer=None, readers=frozenset({FIRST}))
    assert (log.commit(reader), log.latest()) == (False, reader)


@pytest.mark.requires_hard_links
@pytest.mark.parametrize("generation", [pytest.param(2, id="adjacent"), pytest.param(5, id="gap")])
def test_a_replacement_log_keeps_its_writer_when_a_reader_stages_an_old_snapshot(
    lock_file: str, files: OsFiles, root: str, mocker: MockerFixture, generation: int
) -> None:
    old = committed(files, lock_file, root, [None] * generation)
    create = files.create

    def reset_then_create(path: str, data: bytes, *, durable: bool = False) -> tuple[int, int]:
        Path(root).rename(f"{root}.old")
        committed(OsFiles(lock_file), lock_file, root, [None, SECOND])
        return create(path, data, durable=durable)

    mocker.patch.object(files, "create", autospec=True, side_effect=reset_then_create)
    stale = Snapshot(generation=generation + 1, writer=None, readers=frozenset({FIRST}))
    assert not old.commit(stale)
    assert (old.latest(), Path(root, "gen", f"{stale.generation:020d}").exists()) == (
        Snapshot(generation=2, writer=SECOND, readers=frozenset()),
        False,
    )


@pytest.mark.requires_hard_links
def test_a_staged_snapshot_is_lost_when_its_directory_is_renamed(
    lock_file: str, files: OsFiles, root: str, mocker: MockerFixture
) -> None:
    old = committed(files, lock_file, root, [None])
    link = files.link

    def reset_then_link(source: str, target: str) -> bool:
        Path(root).rename(f"{root}.old")
        committed(OsFiles(lock_file), lock_file, root, [SECOND])
        return link(source, target)

    mocker.patch.object(files, "link", autospec=True, side_effect=reset_then_link)
    assert not old.commit(Snapshot(generation=2, writer=None, readers=frozenset({FIRST})))
    assert old.latest() == Snapshot(generation=1, writer=SECOND, readers=frozenset())


@pytest.mark.requires_hard_links
def test_a_missing_link_dependency_leaves_the_previous_generation(
    lock_file: str, files: OsFiles, root: str, mocker: MockerFixture
) -> None:
    log = committed(files, lock_file, root, [SECOND])
    mocker.patch.object(files, "link", autospec=True, side_effect=FileNotFoundError("link failed"))
    with pytest.raises(FileNotFoundError, match="link failed"):
        log.commit(Snapshot(generation=2, writer=None, readers=frozenset({FIRST})))
    assert log.latest() == Snapshot(generation=1, writer=SECOND, readers=frozenset())


@pytest.mark.requires_hard_links
def test_a_late_head_rename_never_moves_head_back(
    lock_file: str, files: OsFiles, root: str, head: Path, mocker: MockerFixture
) -> None:
    log = committed(files, lock_file, root, [None])
    real_link = files.link

    def link_then_peers_commit(source: str, target: str) -> bool:
        landed = real_link(source, target)
        committed(OsFiles(lock_file), lock_file, root, [None] * 3)
        return landed

    mocker.patch.object(files, "link", autospec=True, side_effect=link_then_peers_commit)
    assert (log.commit(Snapshot(generation=2, writer=FIRST, readers=frozenset())), _published(head)) == (
        True,
        Snapshot(generation=5, writer=None, readers=frozenset()),
    )


@pytest.mark.requires_hard_links
def test_a_failed_head_rename_keeps_the_commit(
    lock_file: str, files: OsFiles, root: str, mocker: MockerFixture
) -> None:
    log = committed(files, lock_file, root, [None])
    mocker.patch.object(files, "write_head", autospec=True, side_effect=OSError(EIO, "rename failed"))
    assert (
        log.commit(Snapshot(generation=2, writer=FIRST, readers=frozenset())),
        GenerationLog(OsFiles(lock_file), lock_file, root).latest().writer,
    ) == (True, FIRST)


@pytest.mark.requires_hard_links
def test_missing_directory_while_staging_head_stays_removed(
    lock_file: str, files: OsFiles, root: str, mocker: MockerFixture
) -> None:
    log = committed(files, lock_file, root, [None])
    write_head = files.write_head

    def remove_before_head(path: str, temporary: str, data: bytes) -> None:
        Path(path).parent.rename(Path(root, "removed"))
        write_head(path, temporary, data)

    mocker.patch.object(files, "write_head", autospec=True, side_effect=remove_before_head)
    assert log.commit(Snapshot(generation=2, writer=FIRST, readers=frozenset()))
    assert not Path(root, "gen").exists()


@pytest.mark.requires_hard_links
def test_a_log_whose_epoch_was_never_written_runs_without_head(
    lock_file: str, files: OsFiles, root: str, head: Path
) -> None:
    # A crash can leave the epoch file empty.
    files.prepare(root)
    Path(root, "gen", "epoch").write_bytes(b"")
    log = committed(files, lock_file, root, [None, FIRST])
    assert (head.exists(), log.latest().writer) == (False, FIRST)
