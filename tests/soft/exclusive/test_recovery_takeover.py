from __future__ import annotations

import os
import sys
from contextlib import suppress
from errno import EACCES, EINVAL
from multiprocessing import get_context
from pathlib import Path
from typing import TYPE_CHECKING, Final

import pytest

from filelock import MarkerSoftFileLock, SoftFileLease, SoftFileLock, Timeout
from filelock._filesystem import exchange_names
from filelock._identity import process_start_token
from filelock._soft import STRICT_SOFT_SENTINEL_RECORD
from tests.capability_marks import (
    NEEDS_FOREIGN_STICKY_TMP,
    NEEDS_STALE_RECLAIM,
)
from tests.conftest import DEAD_OWNER, STALE_BREAKERS
from tests.process_helpers import break_then_hang_after_exchange, cleanup_processes

if TYPE_CHECKING:
    from collections.abc import Callable

    from pytest_mock import MockerFixture

from tests.soft.exclusive.recovery_helpers import (
    DEAD_PID,
    HOST,
    assert_times_out,
    break_contents,
    holder_record,
    live_record,
    recovery_name,
)

_PROCESS_DEADLINE: Final[int] = 30


@pytest.mark.usefixtures("break_route")
@pytest.mark.parametrize("break_route", ["no-exchange"], indirect=True)
def test_stale_lock_rename_race(lock_path: Path, mocker: MockerFixture) -> None:
    lock_path.write_text(holder_record(DEAD_PID), encoding="utf-8")
    mocker.patch("filelock._identity._process.process_alive", return_value=False, autospec=True)
    mocker.patch.object(Path, "rename", side_effect=FileNotFoundError("already gone"), autospec=True)
    assert_times_out(lock_path)


def test_stale_break_racing_a_successor_admits_no_third_holder(
    lock_path: Path, mocker: MockerFixture, on_break_recheck: Callable[[Path, Callable[[], None]], None]
) -> None:
    lock_path.write_text(holder_record(DEAD_PID), encoding="utf-8")
    successor: Final[SoftFileLock] = SoftFileLock(lock_path, timeout=0)

    def successor_breaks_and_acquires_first() -> None:
        lock_path.unlink()
        successor.acquire()

    on_break_recheck(lock_path, successor_breaks_and_acquires_first)
    assert_times_out(lock_path)
    mocker.stopall()

    assert_times_out(lock_path)
    successor.release()


@pytest.mark.skipif(sys.platform == "win32", reason="Windows cannot exchange two names")
@NEEDS_STALE_RECLAIM
def test_stale_break_keeps_the_lock_when_a_peer_swaps_its_marker_aside(  # pragma: win32 no cover
    lock_path: Path, mocker: MockerFixture
) -> None:
    lock_path.write_text(holder_record(DEAD_PID), encoding="utf-8")
    (peer := lock_path.with_name("peer")).write_text(holder_record(os.getpid()), encoding="utf-8")
    unlink = Path.unlink

    def peer_exchanges_then_unlink(path: Path, *, missing_ok: bool = False) -> None:
        exchange_names(str(lock_path), str(peer))
        unlink(path, missing_ok=missing_ok)

    mocker.patch.object(Path, "unlink", new=peer_exchanges_then_unlink)
    lock: Final[SoftFileLock] = SoftFileLock(lock_path)
    lock.acquire(blocking=False)
    mocker.stopall()
    exchange_names(str(lock_path), str(peer))

    assert (lock.is_locked, lock.is_lock_held_by_us) == (True, True)
    lock.release()
    assert list(lock_path.parent.glob("test.lock*")) == []


@pytest.mark.skipif(sys.platform == "win32", reason="Windows cannot exchange two names")
@STALE_BREAKERS
@NEEDS_STALE_RECLAIM
def test_stale_break_restores_a_live_marker_a_dead_breaker_left_under_its_break_name(  # pragma: win32 no cover
    tmp_path: Path, lock_type: type[SoftFileLock], record: str
) -> None:
    (lock := tmp_path / "test.lock").write_text(record, encoding="utf-8")
    recovery_name(lock).write_text(live_record(record), encoding="utf-8")
    with pytest.raises(Timeout):
        lock_type(lock).acquire(blocking=False)

    assert (lock.read_text(encoding="utf-8"), list(tmp_path.glob("test.lock.break.*"))) == (live_record(record), [])


@NEEDS_FOREIGN_STICKY_TMP  # pragma: needs foreign-sticky-tmp
@pytest.mark.usefixtures("directories_of_another_account")
def test_stale_break_keeps_a_foreign_record_off_the_lock_path(  # pragma: needs foreign-sticky-tmp
    lock_path_in_shared_tmp: Path,
) -> None:
    lock: Final[Path] = lock_path_in_shared_tmp
    stale: Final[str] = holder_record(DEAD_PID)
    lock.write_text(stale, encoding="utf-8")
    break_name: Final[Path] = lock.with_name(f"{lock.name}.break.{DEAD_PID}.{'0' * 32}")
    live: Final[str] = holder_record(os.getpid())
    break_name.write_text(live, encoding="utf-8")
    try:
        with pytest.raises(Timeout):
            SoftFileLock(lock).acquire(blocking=False)
        assert (
            lock.read_text(encoding="utf-8"),
            [path.read_text(encoding="utf-8") for path in lock.parent.glob(f"{lock.name}.break.*")],
        ) == (stale, [live])
    finally:
        for path in lock.parent.glob(f"{lock.name}.*"):
            path.unlink(missing_ok=True)


@pytest.mark.skipif(sys.platform == "win32", reason="Windows cannot exchange two names")
@pytest.mark.skipif(process_start_token(os.getpid()) is None, reason="the process start token is unavailable")
def test_stale_break_keeps_a_live_breakers_name(tmp_path: Path) -> None:  # pragma: win32 no cover
    lock: Final[Path] = tmp_path / "test.lock"
    stale: Final[str] = holder_record(os.getpid(), start=0)
    lock.write_text(stale, encoding="utf-8")
    break_name: Final[Path] = recovery_name(lock, os.getpid())
    live: Final[str] = holder_record(os.getpid(), start=process_start_token(os.getpid()))
    break_name.write_text(live, encoding="utf-8")

    with pytest.raises(Timeout):
        SoftFileLock(lock).acquire(blocking=False)

    assert (lock.read_text(encoding="utf-8"), break_name.read_text(encoding="utf-8")) == (stale, live)


@pytest.mark.skipif(sys.platform == "win32", reason="Windows cannot exchange two names")
@pytest.mark.usefixtures("unchanged_past_grace")
def test_stale_break_keeps_a_live_breakers_placeholder(tmp_path: Path) -> None:  # pragma: win32 no cover
    lock: Final[Path] = tmp_path / "test.lock"
    placeholder: Final[str] = f"filelock/break\n{os.getpid()}\n"
    lock.write_text(placeholder, encoding="utf-8")
    break_name: Final[Path] = recovery_name(lock, os.getpid())
    live: Final[str] = holder_record(os.getpid())
    break_name.write_text(live, encoding="utf-8")
    contender: Final[SoftFileLock] = SoftFileLock(lock)

    with pytest.raises(Timeout):
        contender.acquire(blocking=False)
    with pytest.raises(Timeout):
        contender.acquire(blocking=False)

    assert (lock.read_text(encoding="utf-8"), break_name.read_text(encoding="utf-8")) == (placeholder, live)


@pytest.mark.skipif(sys.platform == "win32", reason="Windows cannot exchange two names")
@STALE_BREAKERS
@NEEDS_STALE_RECLAIM
def test_stale_break_restores_a_marker_of_another_host_with_the_breakers_pid(  # pragma: win32 no cover
    tmp_path: Path, lock_type: type[SoftFileLock], record: str
) -> None:
    (lock := tmp_path / "test.lock").write_text(record, encoding="utf-8")
    foreign: Final[str] = record.replace(HOST, "elsewhere")
    recovery_name(lock).write_text(foreign, encoding="utf-8")
    with pytest.raises(Timeout):
        lock_type(lock).acquire(blocking=False)

    assert (lock.read_text(encoding="utf-8"), list(tmp_path.glob("test.lock.break.*"))) == (foreign, [])


@pytest.mark.skipif(sys.platform == "win32", reason="Windows cannot exchange two names")
@STALE_BREAKERS
@NEEDS_STALE_RECLAIM
def test_stale_break_leaves_a_marker_taken_since_its_first_look_on_the_path(  # pragma: win32 no cover
    tmp_path: Path,
    lock_type: type[SoftFileLock],
    record: str,
    mocker: MockerFixture,
    on_break_recheck: Callable[[Path, Callable[[], None]], None],
) -> None:
    (lock := tmp_path / "test.lock").write_text(record, encoding="utf-8")
    (holder := tmp_path / "holder").write_text(live_record(record), encoding="utf-8")
    exchange = mocker.patch(
        "filelock._soft._exclusive._recovery.exchange_names", side_effect=exchange_names, autospec=True
    )

    def peer_takes_the_marker() -> None:
        holder.replace(lock)

    on_break_recheck(lock, peer_takes_the_marker)
    with pytest.raises(Timeout):
        lock_type(lock).acquire(blocking=False)

    assert (exchange.call_count, lock.read_text(encoding="utf-8"), sorted(tmp_path.iterdir())) == (
        0,
        live_record(record),
        [lock],
    )


@pytest.mark.skipif(sys.platform == "win32", reason="Windows cannot exchange two names")
@STALE_BREAKERS
@NEEDS_STALE_RECLAIM
def test_stale_break_removes_a_stale_marker_a_dead_breaker_left_under_its_break_name(  # pragma: win32 no cover
    tmp_path: Path, lock_type: type[SoftFileLock], record: str
) -> None:
    (lock := tmp_path / "test.lock").write_text(record, encoding="utf-8")
    recovery_name(lock).write_text(record.replace(str(DEAD_PID), str(DEAD_PID - 1)), encoding="utf-8")
    contender: Final[SoftFileLock] = lock_type(lock)
    contender.acquire(blocking=False)

    assert (contender.is_locked, list(tmp_path.glob("test.lock.break.*"))) == (True, [])
    contender.release()


@pytest.mark.skipif(sys.platform == "win32", reason="Windows cannot exchange two names")
@STALE_BREAKERS
@NEEDS_STALE_RECLAIM
def test_stale_break_restores_a_strict_sentinel_a_dead_breaker_left_under_its_break_name(  # pragma: win32 no cover
    tmp_path: Path, lock_type: type[SoftFileLock], record: str
) -> None:
    (lock := tmp_path / "test.lock").write_text(record, encoding="utf-8")
    recovery_name(lock).write_text(STRICT_SOFT_SENTINEL_RECORD, encoding="utf-8")
    with pytest.raises(Timeout):
        lock_type(lock).acquire(blocking=False)

    assert lock.read_text(encoding="utf-8") == STRICT_SOFT_SENTINEL_RECORD


@pytest.mark.skipif(sys.platform == "win32", reason="Windows cannot exchange two names")
@pytest.mark.parametrize("lock_type", [MarkerSoftFileLock, SoftFileLease])
@NEEDS_STALE_RECLAIM
def test_stale_break_restores_a_marker_of_an_unknown_contract_under_its_break_name(  # pragma: win32 no cover
    tmp_path: Path, lock_type: type[MarkerSoftFileLock]
) -> None:
    (lock := tmp_path / "test.lock").write_text(DEAD_OWNER, encoding="utf-8")
    future: Final[str] = DEAD_OWNER.replace(str(DEAD_PID), str(DEAD_PID - 1)).replace("exclusive", "future")
    recovery_name(lock).write_text(future, encoding="utf-8")
    with pytest.raises(Timeout):
        lock_type(lock).acquire(blocking=False)

    assert lock.read_text(encoding="utf-8") == future


@pytest.mark.skipif(sys.platform == "win32", reason="Windows cannot exchange two names")
@pytest.mark.usefixtures("unchanged_past_grace")
@pytest.mark.parametrize(
    "breaker_pid", [pytest.param(DEAD_PID, id="dead-pid"), pytest.param(2**64, id="out-of-range-pid")]
)
@STALE_BREAKERS
@NEEDS_STALE_RECLAIM
def test_stale_break_restores_a_marker_a_dead_breaker_left_beside_its_placeholder(  # pragma: win32 no cover
    tmp_path: Path, lock_type: type[SoftFileLock], record: str, breaker_pid: int
) -> None:
    (lock := tmp_path / "test.lock").write_text(f"filelock/break\n{breaker_pid}\n", encoding="utf-8")
    recovery_name(lock, breaker_pid).write_text(live_record(record), encoding="utf-8")
    contender: Final[SoftFileLock] = lock_type(lock)
    for _ in range(2):
        with suppress(Timeout):
            contender.acquire(blocking=False)

    assert (contender.is_locked, lock.read_text(encoding="utf-8"), list(tmp_path.glob("test.lock.break.*"))) == (
        False,
        live_record(record),
        [],
    )


@pytest.mark.skipif(sys.platform == "win32", reason="Windows cannot exchange two names")
@pytest.mark.usefixtures("unchanged_past_grace")
@pytest.mark.parametrize(
    "placeholder",
    [
        pytest.param("filelock/break\n", id="no-pid"),
        pytest.param("filelock/break\nnot-a-pid\n", id="not-a-pid"),
        pytest.param(f"filelock/break\n{DEAD_PID}", id="torn-pid"),
    ],
)
@NEEDS_STALE_RECLAIM
def test_stale_break_evicts_a_placeholder_that_names_no_breaker(  # pragma: win32 no cover
    tmp_path: Path, placeholder: str
) -> None:
    (lock := tmp_path / "test.lock").write_text(placeholder, encoding="utf-8")
    recovery_name(lock).write_text(holder_record(os.getpid()), encoding="utf-8")
    contender: Final[SoftFileLock] = SoftFileLock(lock)
    for _ in range(2):
        with suppress(Timeout):
            contender.acquire(blocking=False)

    assert (contender.is_locked, recovery_name(lock).exists()) == (True, True)
    contender.release()


@pytest.mark.skipif(sys.platform == "win32", reason="Windows cannot exchange two names")
@pytest.mark.parametrize("error", [pytest.param(EACCES, id="denied"), pytest.param(EINVAL, id="unsupported")])
def test_stale_break_leaves_a_break_name_it_cannot_exchange(  # pragma: win32 no cover
    lock_path: Path, mocker: MockerFixture, error: int
) -> None:
    lock_path.write_text(holder_record(DEAD_PID), encoding="utf-8")
    recovery_name(lock_path).write_text(holder_record(os.getpid()), encoding="utf-8")
    mocker.patch(
        "filelock._filesystem._exchange._rename_call",
        return_value=(mocker.create_autospec(_exchange_call, return_value=error), 2),
        autospec=True,
    )
    with pytest.raises(Timeout):
        SoftFileLock(lock_path).acquire(blocking=False)

    assert (lock_path.read_text(encoding="utf-8"), break_contents(lock_path)) == (
        holder_record(DEAD_PID),
        [holder_record(os.getpid())],
    )


def _exchange_call(source: bytes, target: bytes, flags: int) -> int:
    raise NotImplementedError  # pragma: no cover  # the autospec answers the call, so only the signature matters


@pytest.mark.skipif(sys.platform == "win32", reason="Windows cannot exchange two names")
def test_stale_break_waits_when_a_break_name_cannot_be_read(  # pragma: win32 no cover
    lock_path: Path, mocker: MockerFixture
) -> None:
    lock_path.write_text(holder_record(DEAD_PID), encoding="utf-8")
    recovery_name(lock_path).write_text(holder_record(os.getpid()), encoding="utf-8")
    open_ = os.open

    def refuse_the_break_name(path: str, flags: int, mode: int = 0o777, *, dir_fd: int | None = None) -> int:
        if ".restore." in os.fspath(path):
            raise OSError(EACCES, "Permission denied", path)
        return open_(path, flags, mode, dir_fd=dir_fd)

    mocker.patch.object(os, "open", new=refuse_the_break_name)
    with pytest.raises(Timeout):
        SoftFileLock(lock_path).acquire(blocking=False)
    mocker.stopall()

    assert (lock_path.read_text(encoding="utf-8"), break_contents(lock_path)) == (
        holder_record(DEAD_PID),
        [holder_record(os.getpid())],
    )


@pytest.mark.skipif(sys.platform == "win32", reason="Windows cannot exchange two names")
@pytest.mark.timeout(_PROCESS_DEADLINE * 2)
@NEEDS_STALE_RECLAIM
def test_stale_break_after_a_breaker_was_killed_keeps_the_holder_alone(  # pragma: win32 no cover
    lock_path: Path,
) -> None:
    lock_path.write_text(holder_record(DEAD_PID), encoding="utf-8")
    context = get_context("spawn")
    ready, go, exchanged = context.Event(), context.Event(), context.Event()
    breaker = context.Process(target=break_then_hang_after_exchange, args=(str(lock_path), ready, go, exchanged))
    holder: Final[SoftFileLock] = SoftFileLock(lock_path)
    with cleanup_processes([breaker]):
        breaker.start()
        assert ready.wait(_PROCESS_DEADLINE)
        holder.acquire(blocking=False)
        go.set()
        assert exchanged.wait(_PROCESS_DEADLINE)
        breaker.terminate()
        breaker.join(_PROCESS_DEADLINE)
        with pytest.raises(Timeout):
            SoftFileLock(lock_path).acquire(blocking=False)

        assert (holder.is_locked, holder.is_lock_held_by_us) == (True, True)
    holder.release()
