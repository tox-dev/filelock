from __future__ import annotations

import shutil
from pathlib import Path
from typing import TYPE_CHECKING, Final

import pytest

from filelock import SoftFileLockProtocolError
from filelock._soft._read_write._generations import GenerationLog, Snapshot
from filelock._soft._read_write._storage import OsFiles

from .conftest import FIRST, SECOND, committed

if TYPE_CHECKING:
    from pytest_mock import MockerFixture

pytestmark: Final[pytest.MarkDecorator] = pytest.mark.usefixtures("flushes")


@pytest.mark.requires_hard_links
@pytest.mark.parametrize(
    ("writers", "expected"),
    [
        pytest.param([], Snapshot(generation=0, writer=None, readers=frozenset()), id="empty"),
        pytest.param([SECOND], Snapshot(generation=1, writer=SECOND, readers=frozenset()), id="restarted"),
    ],
)
def test_a_wiped_log_is_read_from_disk_not_memory(
    lock_file: str, files: OsFiles, root: str, writers: list[str | None], expected: Snapshot
) -> None:
    old = committed(files, lock_file, root, [None, FIRST])
    shutil.rmtree(root)
    committed(OsFiles(lock_file), lock_file, root, writers)
    assert old.latest() == expected


@pytest.mark.requires_hard_links
def test_a_remembered_head_gone_from_the_same_log_fails_closed(lock_file: str, files: OsFiles, root: str) -> None:
    # A matching epoch distinguishes a stale listing from an empty new log.
    old = committed(files, lock_file, root, [None, FIRST])
    for name in (f"{1:020d}", f"{2:020d}", "HEAD"):
        Path(root, "gen", name).unlink()
    with pytest.raises(SoftFileLockProtocolError, match="names no readable snapshot"):
        old.latest()


@pytest.mark.requires_hard_links
def test_an_empty_listing_past_compaction_still_finds_the_remembered_head(
    lock_file: str, files: OsFiles, root: str, head: Path, mocker: MockerFixture
) -> None:
    # Only HEAD or the remembered generation survives beyond the probe window.
    log = committed(files, lock_file, root, [None] * 140 + [FIRST])
    head.unlink()
    mocker.patch.object(OsFiles, "listdir", autospec=True, return_value=[])
    assert log.latest() == Snapshot(generation=141, writer=FIRST, readers=frozenset())


@pytest.mark.requires_hard_links
def test_an_empty_listing_past_compaction_still_finds_the_head_by_name(
    lock_file: str, files: OsFiles, root: str, mocker: MockerFixture
) -> None:
    committed(files, lock_file, root, [None] * 140 + [FIRST])
    mocker.patch.object(OsFiles, "listdir", autospec=True, return_value=[])
    reader = GenerationLog(files, lock_file, root)
    assert reader.latest() == Snapshot(generation=141, writer=FIRST, readers=frozenset())


@pytest.mark.requires_hard_links
def test_a_listing_naming_a_compacted_generation_probes_past_it(
    lock_file: str, files: OsFiles, root: str, mocker: MockerFixture
) -> None:
    committed(files, lock_file, root, [None, None, FIRST])
    mocker.patch.object(OsFiles, "listdir", autospec=True, return_value=[f"{0:020d}"])
    assert GenerationLog(files, lock_file, root).latest().writer == FIRST


@pytest.mark.requires_hard_links
def test_commit_into_a_log_replaced_since_the_read_is_refused(lock_file: str, files: OsFiles, root: str) -> None:
    old = committed(files, lock_file, root, [None, FIRST])
    successor = Snapshot(generation=old.latest().generation + 1, writer=None, readers=frozenset())
    shutil.rmtree(root)
    committed(OsFiles(lock_file), lock_file, root, [SECOND])
    assert not old.commit(successor)
    assert not Path(root, "gen", f"{successor.generation:020d}").exists()


@pytest.mark.requires_hard_links
@pytest.mark.parametrize(
    ("writers", "record"),
    [
        # Two HEAD renames can finish out of order.
        pytest.param(
            [None, None, FIRST], Snapshot(generation=1, writer=None, readers=frozenset()).encode(), id="left-behind"
        ),
        # A crash can tear the HEAD hint.
        pytest.param([None, FIRST], b"garbage\n", id="torn"),
    ],
)
def test_a_head_record_only_says_where_to_look(
    lock_file: str, files: OsFiles, root: str, head: Path, writers: list[str | None], record: bytes
) -> None:
    committed(files, lock_file, root, writers)
    head.write_bytes(Path(root, "gen", "epoch").read_bytes() + record)
    assert GenerationLog(files, lock_file, root).latest() == Snapshot(
        generation=len(writers), writer=FIRST, readers=frozenset()
    )


@pytest.mark.requires_hard_links
def test_a_head_left_on_a_compacted_generation_fails_closed(
    lock_file: str, files: OsFiles, root: str, head: Path, mocker: MockerFixture
) -> None:
    # A delayed rename can point HEAD at a compacted generation.
    committed(files, lock_file, root, [None] * 200)
    head.write_bytes(
        Path(root, "gen", "epoch").read_bytes() + Snapshot(generation=2, writer=None, readers=frozenset()).encode()
    )
    mocker.patch.object(OsFiles, "listdir", autospec=True, return_value=[f"{1:020d}", f"{2:020d}"])
    with pytest.raises(SoftFileLockProtocolError, match="names no readable snapshot"):
        GenerationLog(files, lock_file, root).latest()


@pytest.mark.requires_hard_links
@pytest.mark.parametrize("generation", [pytest.param(1, id="first"), pytest.param(3, id="compacted")])
def test_commit_into_a_slot_the_log_moved_past_is_refused(
    lock_file: str, files: OsFiles, root: str, generation: int
) -> None:
    log = committed(files, lock_file, root, [None] * 70)
    assert (
        log.commit(Snapshot(generation=generation, writer=FIRST, readers=frozenset())),
        Path(root, "gen", f"{generation:020d}").exists(),
    ) == (False, False)


@pytest.mark.requires_hard_links
def test_a_head_from_a_wiped_log_names_nothing_in_the_new_one(
    lock_file: str, files: OsFiles, root: str, head: Path
) -> None:
    committed(files, lock_file, root, [None, None, FIRST])
    stray = head.read_bytes()
    shutil.rmtree(root)
    committed(OsFiles(lock_file), lock_file, root, [SECOND])
    head.write_bytes(stray)
    assert GenerationLog(files, lock_file, root).latest() == Snapshot(generation=1, writer=SECOND, readers=frozenset())
