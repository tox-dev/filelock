from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Final

import pytest

from filelock._soft._read_write import _generations as protocol_mod
from filelock._soft._read_write._generations import Snapshot
from filelock._soft._read_write._storage import OsFiles

from .conftest import FIRST, SECOND, committed, make_participant

if TYPE_CHECKING:
    from pytest_mock import MockerFixture, MockType

pytestmark: Final[pytest.MarkDecorator] = pytest.mark.usefixtures("flushes")


@pytest.mark.requires_hard_links
def test_compaction_removes_a_stalled_survivor_before_the_window_edge(
    lock_file: str, files: OsFiles, root: str
) -> None:
    # Removing gen/6 alone would leave a gap above gen/5.
    log = committed(files, lock_file, root, [None] * 70)
    Path(root, "gen", f"{5:020d}").write_bytes(Snapshot(generation=5, writer=None, readers=frozenset()).encode())
    assert (
        log.commit(Snapshot(generation=71, writer=None, readers=frozenset())),
        min(int(entry.name) for entry in Path(root, "gen").iterdir() if entry.name.isdigit()),
    ) == (True, 7)


@pytest.mark.requires_hard_links
def test_a_listing_removes_old_generations_a_batch_at_a_time(
    lock_file: str, files: OsFiles, root: str, mocker: MockerFixture
) -> None:
    # After seventy commits gen/6 is outside the retention window.
    log = committed(files, lock_file, root, [None] * 70)
    mocker.patch.object(protocol_mod, "HOUSEKEEPING_BATCH", 2)
    for generation in range(1, 6):
        Path(root, "gen", f"{generation:020d}").touch()
    oldest: list[int] = []
    for _ in range(3):
        log.latest()
        oldest.append(min(int(entry.name) for entry in Path(root, "gen").iterdir() if entry.name.isdigit()))
    assert oldest == [3, 5, 7]


@pytest.mark.requires_hard_links
def test_compaction_leaves_more_survivors_than_a_batch_to_the_listing(
    lock_file: str, files: OsFiles, root: str, mocker: MockerFixture
) -> None:
    # Pruning must stay bounded even with many records below the retention window.
    log = committed(files, lock_file, root, [None] * 70)
    mocker.patch.object(protocol_mod, "HOUSEKEEPING_BATCH", 2)
    for generation in (3, 4, 5):
        Path(root, "gen", f"{generation:020d}").touch()
    assert (
        log.commit(Snapshot(generation=71, writer=None, readers=frozenset())),
        sorted(int(entry.name) for entry in Path(root, "gen").iterdir() if entry.name.isdigit())[:4],
    ) == (True, [3, 4, 5, 6])


@pytest.mark.requires_hard_links
def test_a_commit_flushes_only_its_generation_record(
    lock_file: str, files: OsFiles, root: str, flushes: MockType
) -> None:
    log = committed(files, lock_file, root, [None])
    flushes.reset_mock()
    assert (log.commit(Snapshot(generation=2, writer=FIRST, readers=frozenset())), flushes.call_count) == (True, 1)


@pytest.mark.requires_hard_links
def test_a_torn_head_still_refuses_a_first_generation(lock_file: str, files: OsFiles, root: str, head: Path) -> None:
    # The epoch line survives a torn HEAD write.
    log = committed(files, lock_file, root, [None, None, FIRST])
    for generation in (1, 2, 3):
        Path(root, "gen", f"{generation:020d}").unlink()
    head.write_bytes(Path(root, "gen", "epoch").read_bytes() + b"garbage\n")
    assert not log.commit(Snapshot(generation=1, writer=SECOND, readers=frozenset()))


@pytest.mark.requires_hard_links
def test_early_commits_unlink_no_negative_generation(lock_file: str, files: OsFiles, mocker: MockerFixture) -> None:
    unlink = mocker.spy(OsFiles, "unlink")
    writer = make_participant(files, lock_file, "write")
    writer.publish()
    assert writer.advance()
    writer.leave()
    assert [call for call in unlink.call_args_list if Path(call.args[-1]).name.startswith("-")] == []


@pytest.mark.requires_hard_links
def test_latest_on_a_current_head_reads_it_then_probes_one_name(
    lock_file: str, files: OsFiles, root: str, mocker: MockerFixture
) -> None:
    log = committed(files, lock_file, root, [None, FIRST])
    read = mocker.spy(OsFiles, "read")
    assert log.latest().generation == 2
    assert [name for call in read.call_args_list if (name := Path(call.args[-1]).name).isdigit()] == [
        f"{2:020d}",
        f"{3:020d}",
    ]
