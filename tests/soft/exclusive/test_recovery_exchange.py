from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING, Final

import pytest

from filelock import MarkerSoftFileLock, SoftFileLock, Timeout
from filelock._filesystem import exchange_names
from tests.capability_marks import (
    NEEDS_STALE_RECLAIM,
)
from tests.conftest import DEAD_OWNER, STALE_BREAKERS

if TYPE_CHECKING:
    from collections.abc import Callable

    from pytest_mock import MockerFixture

from tests.soft.exclusive.recovery_helpers import HOST, break_contents, live_record, recovery_name


@pytest.mark.skipif(sys.platform == "win32", reason="Windows cannot exchange two names")
@STALE_BREAKERS
@NEEDS_STALE_RECLAIM
def test_stale_break_undoes_an_exchange_that_took_a_peers_marker(  # pragma: win32 no cover
    tmp_path: Path, lock_type: type[SoftFileLock], record: str, mocker: MockerFixture
) -> None:
    (lock := tmp_path / "test.lock").write_text(record, encoding="utf-8")
    foreign: Final[str] = record.replace(HOST, "elsewhere")
    recovery_name(lock).write_text(foreign, encoding="utf-8")
    (peer := tmp_path / "peer").write_text(live_record(record), encoding="utf-8")
    exchange = exchange_names

    def peer_takes_the_path_then_exchange(first: str, second: str) -> bool:
        mocker.patch("filelock._soft._exclusive._recovery.exchange_names", new=exchange)
        peer.replace(lock)
        return exchange(first, second)

    mocker.patch("filelock._soft._exclusive._recovery.exchange_names", new=peer_takes_the_path_then_exchange)
    with pytest.raises(Timeout):
        lock_type(lock).acquire(blocking=False)

    assert (
        lock.read_text(encoding="utf-8"),
        break_contents(lock),
        list(tmp_path.glob("test.lock.restore.*")),
    ) == (live_record(record), [foreign], [])


@pytest.mark.skipif(sys.platform == "win32", reason="Windows cannot exchange two names")
@STALE_BREAKERS
@NEEDS_STALE_RECLAIM
def test_stale_break_restores_a_live_holder_after_interrupted_exchange(  # pragma: win32 no cover
    tmp_path: Path, lock_type: type[SoftFileLock], record: str, mocker: MockerFixture
) -> None:
    (lock := tmp_path / "test.lock").write_text(record, encoding="utf-8")
    (holder := tmp_path / "holder").write_text(live_record(record), encoding="utf-8")

    def exchange_then_interrupt(aside: str, path: str) -> bool:
        holder.replace(lock)
        assert exchange_names(aside, path)
        raise KeyboardInterrupt

    mocker.patch("filelock._soft._exclusive._recovery.exchange_names", new=exchange_then_interrupt)
    with pytest.raises(KeyboardInterrupt):
        lock_type(lock).acquire(blocking=False)

    assert (lock.read_text(encoding="utf-8"), sorted(tmp_path.iterdir())) == (live_record(record), [lock])
    with pytest.raises(Timeout):
        lock_type(lock).acquire(blocking=False)


@pytest.mark.skipif(sys.platform == "win32", reason="Windows cannot exchange two names")
@STALE_BREAKERS
@NEEDS_STALE_RECLAIM
def test_stale_break_exchanges_back_after_the_breaker_that_took_its_marker(  # pragma: win32 no cover
    tmp_path: Path, lock_type: type[SoftFileLock], record: str, mocker: MockerFixture
) -> None:
    (lock := tmp_path / "test.lock").write_text(record, encoding="utf-8")
    (peer := tmp_path / "peer").write_text(record.replace(HOST, "elsewhere"), encoding="utf-8")

    def peer_exchanges_back(_seconds: float) -> None:
        exchange_names(str(peer), str(lock))
        peer.unlink()

    _lose_to_a_live_holder(lock, live_record(record), mocker, lambda _aside, path: exchange_names(str(peer), path))
    mocker.patch(
        "filelock._filesystem._exchange.time",
        autospec=True,
        wraps=time,
        monotonic=mocker.create_autospec(time.monotonic, side_effect=time.monotonic),
        sleep=mocker.create_autospec(time.sleep, side_effect=peer_exchanges_back),
    )
    with pytest.raises(Timeout):
        lock_type(lock).acquire(blocking=False)

    assert (lock.read_text(encoding="utf-8"), sorted(tmp_path.iterdir())) == (live_record(record), [lock])


def _lose_to_a_live_holder(  # pragma: win32 no cover
    lock: Path, live: str, mocker: MockerFixture, then: Callable[[str, str], bool | None]
) -> None:
    (holder := lock.with_name("holder")).write_text(live, encoding="utf-8")

    def exchange_after_the_holder_won(aside: str, path: str) -> bool:
        mocker.patch("filelock._soft._exclusive._recovery.exchange_names", new=exchange_names)
        holder.replace(lock)
        exchange_names(aside, path)
        then(aside, path)
        return True

    mocker.patch("filelock._soft._exclusive._recovery.exchange_names", new=exchange_after_the_holder_won)


@pytest.mark.skipif(sys.platform == "win32", reason="Windows cannot exchange two names")
@STALE_BREAKERS
@NEEDS_STALE_RECLAIM
def test_stale_break_opens_a_ring_of_breakers_holding_each_others_markers(  # pragma: win32 no cover
    tmp_path: Path, lock_type: type[SoftFileLock], record: str, mocker: MockerFixture
) -> None:
    (lock := tmp_path / "test.lock").write_text(record, encoding="utf-8")
    foreign: Final[str] = record.replace(HOST, "elsewhere")
    (peer := tmp_path / "peer").write_text(foreign, encoding="utf-8")

    def peer_then_our_exchange_out_of_turn(aside: str, path: str) -> None:
        exchange_names(str(peer), path)
        exchange_names(aside, path)

    def peer_exchanges_back_once_it_stands_at_the_path(_seconds: float) -> None:
        if lock.read_text(encoding="utf-8") == foreign:
            exchange_names(str(peer), str(lock))
            peer.unlink()

    _lose_to_a_live_holder(lock, live_record(record), mocker, peer_then_our_exchange_out_of_turn)
    mocker.patch(
        "filelock._filesystem._exchange.time",
        autospec=True,
        wraps=time,
        monotonic=mocker.create_autospec(time.monotonic, side_effect=time.monotonic),
        sleep=mocker.create_autospec(time.sleep, side_effect=peer_exchanges_back_once_it_stands_at_the_path),
    )
    with pytest.raises(Timeout):
        lock_type(lock).acquire(blocking=False)

    assert (lock.read_text(encoding="utf-8"), sorted(tmp_path.iterdir())) == (live_record(record), [lock])


@pytest.mark.skipif(sys.platform == "win32", reason="Windows cannot exchange two names")
@STALE_BREAKERS
@NEEDS_STALE_RECLAIM
def test_stale_break_keeps_every_name_when_the_path_never_comes_back(  # pragma: win32 no cover
    tmp_path: Path, lock_type: type[SoftFileLock], record: str, mocker: MockerFixture
) -> None:
    (lock := tmp_path / "test.lock").write_text(record, encoding="utf-8")
    foreign: Final[str] = record.replace(HOST, "elsewhere")
    (peer := tmp_path / "peer").write_text(foreign, encoding="utf-8")
    _lose_to_a_live_holder(lock, live_record(record), mocker, lambda _aside, path: exchange_names(str(peer), path))
    with pytest.raises(Timeout):
        lock_type(lock).acquire(blocking=False)

    assert sorted([lock.read_text(encoding="utf-8"), *break_contents(lock)]) == sorted([foreign, live_record(record)])


@pytest.mark.skipif(sys.platform == "win32", reason="Windows cannot exchange two names")
@STALE_BREAKERS
@NEEDS_STALE_RECLAIM
def test_stale_break_keeps_its_marker_when_exchange_back_is_refused(  # pragma: win32 no cover
    tmp_path: Path, lock_type: type[SoftFileLock], record: str, mocker: MockerFixture
) -> None:
    (lock := tmp_path / "test.lock").write_text(record, encoding="utf-8")
    _lose_to_a_live_holder(lock, live_record(record), mocker, lambda _aside, _path: None)
    mocker.patch(
        "filelock._filesystem._exchange.exchange_names",
        side_effect=[False, AssertionError("retried refused exchange")],
        autospec=True,
    )
    with pytest.raises(Timeout):
        lock_type(lock).acquire(blocking=False)

    assert (f"{os.getpid()}\n" in lock.read_text(encoding="utf-8"), break_contents(lock)) == (
        True,
        [live_record(record)],
    )


@pytest.mark.skipif(sys.platform == "win32", reason="Windows cannot exchange two names")
@STALE_BREAKERS
@NEEDS_STALE_RECLAIM
def test_stale_break_drops_what_it_took_when_the_holder_released_the_path(  # pragma: win32 no cover
    tmp_path: Path, lock_type: type[SoftFileLock], record: str, mocker: MockerFixture
) -> None:
    (lock := tmp_path / "test.lock").write_text(record, encoding="utf-8")
    _lose_to_a_live_holder(lock, live_record(record), mocker, lambda _aside, path: Path(path).unlink())
    with pytest.raises(Timeout):
        lock_type(lock).acquire(blocking=False)

    assert list(tmp_path.iterdir()) == []


@pytest.mark.skipif(sys.platform == "win32", reason="Windows cannot exchange two names")
@NEEDS_STALE_RECLAIM
def test_stale_break_cleans_its_marker_when_the_break_name_becomes_a_directory(  # pragma: win32 no cover
    tmp_path: Path, mocker: MockerFixture
) -> None:
    (lock := tmp_path / "test.lock").write_text(DEAD_OWNER, encoding="utf-8")
    (holder := tmp_path / "holder").write_text(live_record(DEAD_OWNER), encoding="utf-8")
    aside: str | None = None
    missing = 0
    lstat = os.lstat

    def holder_moves_the_break_name(break_name: str, path: str) -> bool:
        nonlocal aside
        aside = break_name
        mocker.patch("filelock._soft._exclusive._recovery.exchange_names", new=exchange_names)
        holder.replace(lock)
        exchange_names(break_name, path)
        Path(break_name).rename(tmp_path / "taken")
        return True

    def replace_the_missing_break_name(path: str, *, dir_fd: int | None = None) -> os.stat_result:
        nonlocal missing
        try:
            return lstat(path, dir_fd=dir_fd)
        except FileNotFoundError:
            if path == aside:
                missing += 1
                if missing == 2:
                    Path(path).mkdir()
            raise

    mocker.patch("filelock._soft._exclusive._recovery.exchange_names", new=holder_moves_the_break_name)
    mocker.patch.object(os, "lstat", new=replace_the_missing_break_name)
    with pytest.raises(Timeout):
        MarkerSoftFileLock(lock).acquire(blocking=False)

    assert (missing, lock.exists(), (tmp_path / "taken").read_text(encoding="utf-8")) == (
        2,
        False,
        live_record(DEAD_OWNER),
    )
    with MarkerSoftFileLock(lock, blocking=False) as acquired:
        assert acquired.is_locked
