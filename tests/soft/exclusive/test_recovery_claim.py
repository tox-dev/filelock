from __future__ import annotations

import os
import sys
import threading
from contextlib import suppress
from errno import EACCES, EIO
from pathlib import Path
from typing import TYPE_CHECKING, Final

import pytest

from filelock import SoftFileLock, Timeout
from filelock._filesystem import exchange_names
from filelock._soft import read_lock_file
from filelock._soft._exclusive._recovery import _begin_claim, _end_claim
from tests.capability_marks import (
    NEEDS_STALE_RECLAIM,
)
from tests.conftest import STALE_BREAKERS, held_after_reclaim

if TYPE_CHECKING:
    from pytest_mock import MockerFixture

from tests.soft.exclusive.recovery_helpers import DEAD_PID, HOST, break_contents, recovery_name


@pytest.mark.skipif(sys.platform == "win32", reason="Windows cannot exchange two names")
@STALE_BREAKERS
@NEEDS_STALE_RECLAIM
def test_stale_break_restores_under_an_exclusive_claim(  # pragma: win32 no cover
    tmp_path: Path, lock_type: type[SoftFileLock], record: str, mocker: MockerFixture
) -> None:
    (lock := tmp_path / "test.lock").write_text(record, encoding="utf-8")
    foreign: Final[str] = record.replace(HOST, "elsewhere")
    recovery_name(lock).write_text(foreign, encoding="utf-8")
    rival_settled: Final[threading.Event] = threading.Event()
    release_rival: Final[threading.Event] = threading.Event()
    rival_timeouts: Final[list[bool]] = []

    def rival_recovers() -> None:
        try:
            with pytest.raises(Timeout):
                lock_type(lock).acquire(blocking=False)
            rival_timeouts.append(True)
        finally:
            rival_settled.set()

    rival: Final[threading.Thread] = threading.Thread(target=rival_recovers)
    main: Final[threading.Thread] = threading.current_thread()
    unlink = Path.unlink

    def exchange_with_rival(first: str, second: str) -> bool:
        if threading.current_thread() is main:
            rival.start()
            rival_settled.wait()
            return exchange_names(first, second)
        rival_settled.set()  # pragma: no cover  # only concurrent recovery reaches this branch
        release_rival.wait()  # pragma: no cover  # only concurrent recovery reaches this branch
        return exchange_names(first, second)  # pragma: no cover  # only concurrent recovery reaches this branch

    def unlink_after_the_rival_finished(path: Path, *, missing_ok: bool = False) -> None:
        release_rival.set()
        rival.join()
        unlink(path, missing_ok=missing_ok)

    mocker.patch("filelock._soft._exclusive._recovery.exchange_names", new=exchange_with_rival)
    mocker.patch.object(Path, "unlink", new=unlink_after_the_rival_finished)
    with pytest.raises(Timeout):
        lock_type(lock).acquire(blocking=False)
    release_rival.set()
    rival.join()

    assert (
        rival_timeouts,
        lock.read_text(encoding="utf-8"),
        list(tmp_path.glob("test.lock.break.*")),
        list(tmp_path.glob("test.lock.restore.*")),
    ) == ([True], foreign, [], [])


@pytest.mark.skipif(sys.platform == "win32", reason="Windows cannot exchange two names")
@STALE_BREAKERS
@NEEDS_STALE_RECLAIM
def test_stale_break_waits_when_a_peer_claimed_the_record_first(  # pragma: win32 no cover
    tmp_path: Path, lock_type: type[SoftFileLock], record: str, mocker: MockerFixture
) -> None:
    (lock := tmp_path / "test.lock").write_text(record, encoding="utf-8")
    foreign: Final[str] = record.replace(HOST, "elsewhere")
    recovery_name(lock).write_text(foreign, encoding="utf-8")
    rename = os.rename

    def peer_claims_first(source: Path, target: str) -> None:
        rename(source, _restore_name(lock, DEAD_PID - 1))
        rename(source, target)

    mocker.patch("os.rename", side_effect=peer_claims_first, autospec=True)
    with pytest.raises(Timeout):
        lock_type(lock).acquire(blocking=False)

    assert (lock.read_text(encoding="utf-8"), _restore_name(lock, DEAD_PID - 1).read_text(encoding="utf-8")) == (
        record,
        foreign,
    )


def _restore_name(lock: Path, pid: int | str) -> Path:  # pragma: win32 no cover
    return lock.with_name(f"test.lock.restore.{pid}.{'0' * 32}")


@pytest.mark.skipif(sys.platform == "win32", reason="Windows cannot exchange two names")
@STALE_BREAKERS
def test_stale_break_waits_when_the_record_cannot_be_claimed(  # pragma: win32 no cover
    tmp_path: Path, lock_type: type[SoftFileLock], record: str, mocker: MockerFixture
) -> None:
    (lock := tmp_path / "test.lock").write_text(record, encoding="utf-8")
    foreign: Final[str] = record.replace(HOST, "elsewhere")
    recovery_name(lock).write_text(foreign, encoding="utf-8")
    mocker.patch("os.rename", side_effect=OSError(EACCES, "denied"), autospec=True)
    with pytest.raises(Timeout):
        lock_type(lock).acquire(blocking=False)

    assert (lock.read_text(encoding="utf-8"), break_contents(lock)) == (record, [foreign])


@pytest.mark.skipif(sys.platform == "win32", reason="Windows cannot exchange two names")
@STALE_BREAKERS
@NEEDS_STALE_RECLAIM
def test_stale_break_reclaims_a_record_a_dead_recoverer_claimed(  # pragma: win32 no cover
    tmp_path: Path, lock_type: type[SoftFileLock], record: str
) -> None:
    (lock := tmp_path / "test.lock").write_text(record, encoding="utf-8")
    foreign: Final[str] = record.replace(HOST, "elsewhere")
    _restore_name(lock, DEAD_PID).write_text(foreign, encoding="utf-8")
    with pytest.raises(Timeout):
        lock_type(lock).acquire(blocking=False)

    assert (lock.read_text(encoding="utf-8"), list(tmp_path.glob("test.lock.restore.*"))) == (foreign, [])


@pytest.mark.skipif(sys.platform == "win32", reason="Windows cannot exchange two names")
@STALE_BREAKERS
def test_stale_break_skips_a_record_a_live_recoverer_claimed(  # pragma: win32 no cover
    tmp_path: Path, lock_type: type[SoftFileLock], record: str
) -> None:
    (lock := tmp_path / "test.lock").write_text(record, encoding="utf-8")
    foreign: Final[str] = record.replace(HOST, "elsewhere")
    (claimed := _restore_name(lock, os.getppid())).write_text(foreign, encoding="utf-8")
    with pytest.raises(Timeout):
        lock_type(lock).acquire(blocking=False)

    assert (lock.read_text(encoding="utf-8"), claimed.read_text(encoding="utf-8")) == (record, foreign)


@pytest.mark.skipif(sys.platform == "win32", reason="Windows cannot exchange two names")
@STALE_BREAKERS
@NEEDS_STALE_RECLAIM
@pytest.mark.parametrize(
    "recoverer",
    [
        pytest.param(str(2**64), id="oversized"),
        pytest.param("0", id="zero"),
        # This process's pid in fullwidth digits, which int() reads back as the same number.
        pytest.param("".join(chr(0xFF10 + int(digit)) for digit in str(os.getpid())), id="wide"),
    ],
)
def test_stale_break_ignores_a_claim_naming_no_valid_pid(  # pragma: win32 no cover
    tmp_path: Path, lock_type: type[SoftFileLock], record: str, recoverer: str
) -> None:
    (lock := tmp_path / "test.lock").write_text(record, encoding="utf-8")
    foreign: Final[str] = record.replace(HOST, "elsewhere")
    (planted := _restore_name(lock, recoverer)).write_text(foreign, encoding="utf-8")
    with held_after_reclaim(lock_type(lock)) as held:
        assert held.is_locked

    assert planted.read_text(encoding="utf-8") == foreign


@pytest.mark.skipif(sys.platform == "win32", reason="Windows cannot exchange two names")
@STALE_BREAKERS
@NEEDS_STALE_RECLAIM
def test_stale_break_retries_a_claim_whose_return_failed(  # pragma: win32 no cover
    tmp_path: Path, lock_type: type[SoftFileLock], record: str, mocker: MockerFixture
) -> None:
    (lock := tmp_path / "test.lock").write_text(record, encoding="utf-8")
    foreign: Final[str] = record.replace(HOST, "elsewhere")
    recovery_name(lock).write_text(foreign, encoding="utf-8")
    _refuse_returning_a_claim(mocker)
    mocker.patch(
        "filelock._soft._exclusive._recovery.exchange_names",
        autospec=True,
        return_value=False,
    )
    with pytest.raises(Timeout):
        lock_type(lock).acquire(blocking=False)
    left: Final[list[Path]] = list(tmp_path.glob(f"test.lock.restore.{os.getpid()}.*"))
    mocker.stopall()
    with pytest.raises(Timeout):
        lock_type(lock).acquire(blocking=False)

    assert (len(left), lock.read_text(encoding="utf-8"), sorted(tmp_path.iterdir())) == (1, foreign, [lock])


def _refuse_returning_a_claim(mocker: MockerFixture) -> None:  # pragma: win32 no cover
    rename = Path.rename

    def rename_unless_returning(path: Path, target: str | Path) -> Path:
        if ".break." in str(target):
            raise OSError(EACCES, "denied")
        return rename(path, target)

    mocker.patch.object(Path, "rename", new=rename_unless_returning)


@pytest.mark.skipif(sys.platform == "win32", reason="Windows cannot exchange two names")
@STALE_BREAKERS
@NEEDS_STALE_RECLAIM
def test_stale_break_finishes_a_claim_an_unreadable_record_interrupted(  # pragma: win32 no cover
    tmp_path: Path, lock_type: type[SoftFileLock], record: str, mocker: MockerFixture
) -> None:
    (lock := tmp_path / "test.lock").write_text(record, encoding="utf-8")
    foreign: Final[str] = record.replace(HOST, "elsewhere")
    recovery_name(lock).write_text(foreign, encoding="utf-8")
    read = read_lock_file

    def read_unless_claimed(path: str) -> tuple[str | None, os.stat_result]:
        if ".restore." in path:
            raise OSError(EIO, "unreadable")
        return read(path)

    _refuse_returning_a_claim(mocker)
    mocker.patch("filelock._soft._exclusive._recovery.read_lock_file", new=read_unless_claimed)
    with pytest.raises(Timeout):
        lock_type(lock).acquire(blocking=False)
    left: Final[list[Path]] = list(tmp_path.glob(f"test.lock.restore.{os.getpid()}.*"))
    mocker.stopall()
    with pytest.raises(Timeout):
        lock_type(lock).acquire(blocking=False)

    assert (len(left), lock.read_text(encoding="utf-8"), sorted(tmp_path.iterdir())) == (1, foreign, [lock])


@pytest.mark.skipif(sys.platform == "win32", reason="Windows cannot exchange two names")
@STALE_BREAKERS
@NEEDS_STALE_RECLAIM
def test_stale_break_waits_for_a_claim_another_thread_is_working(  # pragma: win32 no cover
    tmp_path: Path, lock_type: type[SoftFileLock], record: str, mocker: MockerFixture
) -> None:
    (lock := tmp_path / "test.lock").write_text(record, encoding="utf-8")
    recovery_name(lock).write_text(record.replace(HOST, "elsewhere"), encoding="utf-8")
    working, release = threading.Event(), threading.Event()
    exchange = exchange_names

    def exchange_when_released(first: str, second: str) -> bool:
        working.set()
        release.wait(5)
        return exchange(first, second)

    mocker.patch("filelock._soft._exclusive._recovery.exchange_names", new=exchange_when_released)
    worker: Final[threading.Thread] = threading.Thread(target=_acquire, args=(lock_type, lock))
    worker.start()
    assert working.wait(5)
    claims_while_working: Final[list[Path]] = list(tmp_path.glob(f"test.lock.restore.{os.getpid()}.*"))
    with pytest.raises(Timeout):
        lock_type(lock).acquire(blocking=False)
    untouched: Final[list[Path]] = list(tmp_path.glob(f"test.lock.restore.{os.getpid()}.*"))
    release.set()
    worker.join()

    assert (len(claims_while_working), untouched == claims_while_working) == (1, True)


def _acquire(lock_type: type[SoftFileLock], lock: Path) -> None:  # pragma: win32 no cover
    with suppress(Timeout):
        lock_type(lock).acquire(blocking=False)


@pytest.mark.skipif(sys.platform == "win32", reason="Windows cannot exchange two names")
@STALE_BREAKERS
@NEEDS_STALE_RECLAIM
def test_stale_break_claim_is_registered_before_it_is_visible(  # pragma: win32 no cover
    tmp_path: Path, lock_type: type[SoftFileLock], record: str, mocker: MockerFixture
) -> None:
    (lock := tmp_path / "test.lock").write_text(record, encoding="utf-8")
    foreign: Final[str] = record.replace(HOST, "elsewhere")
    recovery_name(lock).write_text(foreign, encoding="utf-8")
    rename = Path.rename
    rival_timeouts: Final[list[bool]] = []
    claims_after_rival: Final[list[list[Path]]] = []

    def rival_scans_after_the_claim_rename(path: Path, target: str | Path) -> Path:
        renamed: Final[Path] = rename(path, target)
        mocker.stopall()
        rival: Final[threading.Thread] = threading.Thread(
            target=_acquire_timing_out, args=(lock_type, lock, rival_timeouts)
        )
        rival.start()
        rival.join()
        claims_after_rival.append(list(tmp_path.glob("test.lock.restore.*")))
        return renamed

    mocker.patch.object(Path, "rename", new=rival_scans_after_the_claim_rename)
    with pytest.raises(Timeout):
        lock_type(lock).acquire(blocking=False)

    assert (
        rival_timeouts,
        len(claims_after_rival[0]),
        lock.read_text(encoding="utf-8"),
        sorted(tmp_path.iterdir()),
    ) == (
        [True],
        1,
        foreign,
        [lock],
    )


def _acquire_timing_out(  # pragma: win32 no cover
    lock_type: type[SoftFileLock], lock: Path, outcomes: list[bool]
) -> None:
    with pytest.raises(Timeout):
        lock_type(lock).acquire(blocking=False)
    outcomes.append(True)


@pytest.mark.skipif(sys.platform == "win32", reason="Windows cannot exchange two names")
@STALE_BREAKERS
def test_stale_break_stops_when_the_claim_is_already_registered(  # pragma: win32 no cover
    tmp_path: Path, lock_type: type[SoftFileLock], record: str, mocker: MockerFixture
) -> None:
    (lock := tmp_path / "test.lock").write_text(record, encoding="utf-8")
    foreign: Final[str] = record.replace(HOST, "elsewhere")
    recovery_name(lock).write_text(foreign, encoding="utf-8")
    mocker.patch("filelock._soft._exclusive._recovery.secrets.token_hex", return_value="a" * 32, autospec=True)
    registered: Final[str] = f"{lock!s}.restore.{os.getpid()}.{'a' * 32}"
    assert _begin_claim(registered)
    try:
        with pytest.raises(Timeout):
            lock_type(lock).acquire(blocking=False)
    finally:
        _end_claim(registered)

    assert (lock.read_text(encoding="utf-8"), break_contents(lock)) == (record, [foreign])


@pytest.mark.skipif(sys.platform == "win32", reason="Windows cannot exchange two names")
@STALE_BREAKERS
def test_stale_break_claim_identity_ignores_path_spelling(  # pragma: win32 no cover
    tmp_path: Path, lock_type: type[SoftFileLock], record: str, mocker: MockerFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    (lock := tmp_path / "test.lock").write_text(record, encoding="utf-8")
    foreign: Final[str] = record.replace(HOST, "elsewhere")
    recovery_name(lock).write_text(foreign, encoding="utf-8")
    mocker.patch("filelock._soft._exclusive._recovery.secrets.token_hex", return_value="a" * 32, autospec=True)
    registered: Final[Path] = Path(f"{lock}.restore.{os.getpid()}.{'a' * 32}")
    assert _begin_claim(registered)
    monkeypatch.chdir(tmp_path)
    try:
        with pytest.raises(Timeout):
            lock_type("./test.lock").acquire(blocking=False)
    finally:
        _end_claim(registered)

    assert (lock.read_text(encoding="utf-8"), break_contents(lock)) == (record, [foreign])


@pytest.mark.skipif(sys.platform == "win32", reason="Windows cannot exchange two names")
@pytest.mark.parametrize("interrupted", ["rename", "read", "exchange"])
@STALE_BREAKERS
@NEEDS_STALE_RECLAIM
def test_stale_break_releases_the_registration_when_recovery_is_interrupted(  # pragma: win32 no cover
    tmp_path: Path, lock_type: type[SoftFileLock], record: str, interrupted: str, mocker: MockerFixture
) -> None:
    (lock := tmp_path / "test.lock").write_text(record, encoding="utf-8")
    foreign: Final[str] = record.replace(HOST, "elsewhere")
    recovery_name(lock).write_text(foreign, encoding="utf-8")
    rename = Path.rename

    def rename_then_interrupt(path: Path, target: str | Path) -> Path:
        rename(path, target)
        raise KeyboardInterrupt

    def read_the_claim_then_interrupt(path: str) -> tuple[str | None, os.stat_result]:
        if ".restore." in path:
            raise KeyboardInterrupt
        return read_lock_file(path)

    def exchange_then_interrupt(*_names: str) -> bool:
        raise KeyboardInterrupt

    target, interruption = {
        "rename": ("pathlib.Path.rename", rename_then_interrupt),
        "read": ("filelock._soft._exclusive._recovery.read_lock_file", read_the_claim_then_interrupt),
        "exchange": ("filelock._soft._exclusive._recovery.exchange_names", exchange_then_interrupt),
    }[interrupted]
    mocker.patch(target, new=interruption)
    with pytest.raises(KeyboardInterrupt):
        lock_type(lock).acquire(blocking=False)
    mocker.stopall()
    (claim,) = tmp_path.glob("test.lock.restore.*")
    released: Final[bool] = _begin_claim(claim)
    _end_claim(claim)
    with pytest.raises(Timeout):
        lock_type(lock).acquire(blocking=False)

    assert (released, lock.read_text(encoding="utf-8"), sorted(tmp_path.iterdir())) == (True, foreign, [lock])
