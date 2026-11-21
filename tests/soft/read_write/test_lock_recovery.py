from __future__ import annotations

import shutil
import time
from pathlib import Path
from typing import TYPE_CHECKING, Final

import pytest

from filelock import SoftFileLockProtocolError, SoftReadWriteLock, Timeout
from filelock._soft._read_write._generations import GenerationLog
from filelock._soft._read_write._participant import Participant
from filelock._soft._read_write._storage import OsFiles

from .lock_helpers import (
    PROCESS_DEADLINE,
    generations,
    holders,
    make_lock,
    plant_holder,
    read_state,
    wait_until,
    writer_gets_in,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from contextlib import AbstractContextManager

    from pytest_mock import MockerFixture, MockType

pytestmark: Final[list[pytest.MarkDecorator]] = [
    pytest.mark.requires_hard_links,
    pytest.mark.usefixtures("clear_singletons", "flushes"),
]


def test_short_attempts_still_evict_a_dead_holder(lock_file: str) -> None:
    plant_holder(lock_file, mode="write")
    lock = make_lock(lock_file, stale_threshold=0.3)
    try:

        def attempt() -> bool:
            try:
                lock.acquire_read(blocking=False)
            except Timeout:
                return False
            return True

        wait_until(attempt)
        lock.release()
    finally:
        lock.close()


def test_generation_is_a_fencing_token(lock_file: str) -> None:
    first = make_lock(lock_file)
    second = make_lock(lock_file)
    try:
        assert first.generation is None
        with first.write_lock(timeout=2):
            granted = first.generation
            assert granted is not None
        with second.write_lock(timeout=2):
            later = second.generation
            assert later is not None
            assert later > granted
        assert second.generation is None
    finally:
        first.close()
        second.close()


def test_foreign_host_holder_is_evicted_after_the_stale_threshold(lock_file: str) -> None:
    # A replacement pod can have a different hostname (#725).
    plant_holder(lock_file, mode="write")
    lock = make_lock(lock_file, stale_threshold=0.3)
    try:
        started = time.monotonic()
        with pytest.raises(Timeout):
            lock.acquire_read(timeout=0.1)
        # The first attempt already started watching the record, so the wait spans both attempts.
        with lock.read_lock(timeout=5):
            assert time.monotonic() - started >= 0.3 - 0.05
        assert holders(lock_file) == []
    finally:
        lock.close()


def test_release_never_blocks_on_an_abandoned_claim(lock_file: str) -> None:
    # An abandoned record must not block a live writer from leaving (#725).
    lock = make_lock(lock_file, heartbeat_interval=10, stale_threshold=40)
    lock.acquire_write(timeout=2)
    plant_holder(lock_file, mode="read")
    started = time.monotonic()
    lock.release()
    assert time.monotonic() - started < 1
    assert read_state(lock_file).writer is None
    lock.close()


def test_malformed_generation_record_fails_closed(lock_file: str, lock: SoftReadWriteLock) -> None:
    with lock.write_lock(timeout=2):
        pass
    Path(f"{lock_file}.rw", "gen", f"{int(generations(lock_file)[-1]) + 1:020d}").write_bytes(b"garbage\n")
    with pytest.raises(SoftFileLockProtocolError, match="malformed generation record"):
        lock.acquire_write(timeout=1)


def test_removing_the_log_recovers_without_admitting_a_second_writer(lock_file: str, lock: SoftReadWriteLock) -> None:
    # Renaming the log models the documented recovery from a malformed record.
    with lock.write_lock(timeout=2):
        pass
    _remove_log(lock_file)
    peer = make_lock(lock_file)
    try:
        with peer.write_lock(timeout=2):
            assert peer.generation == 1
            with pytest.raises(Timeout):
                lock.acquire_write(blocking=False)
        with lock.write_lock(timeout=2):
            assert lock.generation == 3
    finally:
        peer.close()


def _remove_log(lock_file: str) -> None:
    # A single rename removes the log atomically.
    Path(f"{lock_file}.rw").rename(removed := Path(f"{lock_file}.rw.removed"))
    shutil.rmtree(removed)


@pytest.mark.parametrize("missing", ["tree", "gen", "temporary"])
@pytest.mark.parametrize(
    "hold",
    [pytest.param(SoftReadWriteLock.read_lock, id="read"), pytest.param(SoftReadWriteLock.write_lock, id="write")],
)
def test_acquire_after_a_staged_snapshot_disappears(
    lock_file: str,
    lock: SoftReadWriteLock,
    mocker: MockerFixture,
    missing: str,
    hold: Callable[[SoftReadWriteLock, float], AbstractContextManager[None]],
) -> None:
    removed = _remove_at_next_link(lock_file, missing, mocker)
    with hold(lock, 2):
        removed.assert_called_once()
        assert not writer_gets_in(lock_file)
    assert writer_gets_in(lock_file)


def _remove_at_next_link(lock_file: str, missing: str, mocker: MockerFixture) -> MockType:
    link = OsFiles.link

    def remove_before_link(files: OsFiles, source: str, target: str) -> bool:
        mocker.stop(interrupt)
        if missing == "tree":
            _remove_log(lock_file)
        elif missing == "gen":
            shutil.rmtree(Path(source).parent)
        else:
            Path(source).unlink()
        return link(files, source, target)

    interrupt = mocker.patch.object(OsFiles, "link", autospec=True, side_effect=remove_before_link)
    return interrupt


@pytest.mark.parametrize("missing", ["tree", "gen", "temporary"])
def test_release_after_a_staged_snapshot_disappears(
    lock_file: str, lock: SoftReadWriteLock, mocker: MockerFixture, missing: str
) -> None:
    lock.acquire_write(timeout=2)
    removed = _remove_at_next_link(lock_file, missing, mocker)
    lock.release()
    removed.assert_called_once()
    assert writer_gets_in(lock_file)


def test_missing_generation_directory_before_staging(
    lock_file: str, lock: SoftReadWriteLock, mocker: MockerFixture
) -> None:
    create = OsFiles.create

    def remove_before_create(files: OsFiles, path: str, data: bytes, *, durable: bool = False) -> tuple[int, int]:
        if Path(path).name.startswith(".commit-"):
            mocker.stop(interrupt)
            shutil.rmtree(Path(path).parent)
        return create(files, path, data, durable=durable)

    interrupt = mocker.patch.object(OsFiles, "create", autospec=True, side_effect=remove_before_create)
    with lock.write_lock(timeout=2):
        assert not writer_gets_in(lock_file)
    assert writer_gets_in(lock_file)


def test_a_contender_waiting_when_the_log_is_removed_starts_over(
    lock_file: str, lock: SoftReadWriteLock, mocker: MockerFixture
) -> None:
    lock.acquire_write(timeout=2)
    root = f"{lock_file}.rw"
    old_epoch = Path(root, "gen", "epoch").read_bytes()
    real_advance = Participant.advance
    removed: list[str] = []

    def remove_once_blocked(participant: Participant) -> bool:
        if not (granted := real_advance(participant)) and not removed:
            _remove_log(lock_file)
            removed.append(participant.token)
        return granted

    mocker.patch.object(Participant, "advance", autospec=True, side_effect=remove_once_blocked)
    waiter = make_lock(lock_file)
    try:
        waiter.acquire_write(timeout=PROCESS_DEADLINE)
        assert removed
        latest = GenerationLog(OsFiles(lock_file), lock_file, root).latest()
        assert Path(root, "gen", "epoch").read_bytes() != old_epoch
        assert latest.generation == waiter.generation
        assert {latest.writer} == {entry.name for entry in Path(root, "holders").iterdir()}
    finally:
        waiter.close()


def test_malformed_holder_record_is_evicted(lock_file: str) -> None:
    Path(f"{lock_file}.rw", "holders", plant_holder(lock_file, mode="write")).write_bytes(b"\x00garbage")
    lock = make_lock(lock_file, stale_threshold=0.3)
    try:
        with lock.write_lock(timeout=5):
            pass
    finally:
        lock.close()


def test_temporary_commit_files_are_swept(lock_file: str) -> None:
    orphan = Path(f"{lock_file}.rw", "gen", ".commit-abandoned")
    lock = make_lock(lock_file, heartbeat_interval=0.05, stale_threshold=0.2)
    try:
        with lock.write_lock(timeout=2):
            pass
        orphan.write_bytes(b"left by a crash between create and link")
        with lock.write_lock(timeout=2):
            wait_until(lambda: not orphan.exists())
        assert not orphan.exists()
    finally:
        lock.close()


def test_generations_are_compacted(lock_file: str, lock: SoftReadWriteLock) -> None:
    for _ in range(80):
        with lock.write_lock(timeout=2):
            pass
    assert len(generations(lock_file)) <= 66


def test_a_participant_behind_a_compacted_generation_rescans(lock_file: str) -> None:
    lock = make_lock(lock_file)
    peer = make_lock(lock_file)
    try:
        with lock.write_lock(timeout=2):
            pass
        stale_log = GenerationLog(OsFiles(lock_file), lock_file, f"{lock_file}.rw")
        remembered = stale_log.latest()
        for _ in range(40):
            with peer.write_lock(timeout=2):
                pass
        assert not Path(f"{lock_file}.rw", "gen", f"{remembered.generation:020d}").exists()
        assert stale_log.latest().generation == read_state(lock_file).generation
    finally:
        lock.close()
        peer.close()
