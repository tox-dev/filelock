from __future__ import annotations

import gc
import os
import sys
import textwrap
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Final, Literal, NoReturn

import pytest

import filelock._sqlite._connection  # simplint: ignore[private-import-path]  # tests internal behavior
from filelock import ReadWriteLock
from tests.capability_marks import (
    NEEDS_AUDIT_EVENTS,
    NEEDS_COLLECTED_FINALIZATION,
)
from tests.descriptor_helpers import descriptors_on
from tests.read_write_helpers import writer_gets_in
from tests.sqlite.process_support import NEEDS_FD_DIRECTORY, run_fork_script

if TYPE_CHECKING:
    from _typeshed import StrOrBytesPath  # simplint: ignore[private-import-path]  # tests internal behavior
    from pytest_mock import MockerFixture

_NEEDS_PROBE_DESCRIPTORS: Final[pytest.MarkDecorator] = pytest.mark.skipif(
    sys.platform == "win32", reason="Windows connects without probe descriptors"
)


@NEEDS_AUDIT_EVENTS
def test_read_write_lock_closes_idle_connections(tmp_path: Path) -> None:
    lock_path = tmp_path / "idle.db"
    connection_events = 0

    # Typed for the sqlite3.connect payload the hook reads; every other event returns before touching args.
    def audit_hook(  # pragma: no cover - interpreter audit hooks run with tracing disabled
        event: str, args: tuple[StrOrBytesPath, ...]
    ) -> None:
        nonlocal connection_events
        if event != "sqlite3.connect":
            return
        database = args[0]
        # The lock connects by its own path, or through a private hard link where others share its directory.
        if isinstance(database, (str, bytes)) and (
            (path := os.fsdecode(database)) == str(lock_path) or Path(path).parent.parent.name.startswith(".filelock-")
        ):
            connection_events += 1

    sys.addaudithook(audit_hook)
    lock = ReadWriteLock(lock_path, is_singleton=False)
    lock.acquire_read()
    lock.acquire_read()
    lock.release()
    lock.release()
    lock.acquire_write()
    lock.release()
    lock_path.unlink()

    assert connection_events == 3


@NEEDS_FD_DIRECTORY
@NEEDS_COLLECTED_FINALIZATION
def test_read_write_lock_dropped_instances_leave_no_descriptors(tmp_path: Path) -> None:  # pragma: needs fd-directory
    result = run_fork_script(
        textwrap.dedent("""
        from __future__ import annotations

        import gc
        import sys
        from pathlib import Path

        from filelock import ReadWriteLock

        root = Path(sys.argv[1])
        descriptor_path = Path("/dev/fd") if Path("/dev/fd").is_dir() else Path("/proc/self/fd")
        gc.collect()
        initial_count = sum(1 for _ in descriptor_path.iterdir())
        for index in range(100):
            lock = ReadWriteLock(root / f"dropped-{index}.db", is_singleton=False)
            lock.acquire_write()
            del lock
        gc.collect()

        assert sum(1 for _ in descriptor_path.iterdir()) == initial_count
        with ReadWriteLock(root / "dropped-0.db", is_singleton=False).read_lock():
            pass
        """),
        [str(tmp_path)],
        timeout=10,
    )

    assert result == (0, "", "")


@NEEDS_FD_DIRECTORY
def test_read_write_lock_keeps_its_probe_descriptor_while_held(tmp_path: Path) -> None:  # pragma: needs fd-directory
    lock_path: Final[Path] = tmp_path / "held.db"
    with ReadWriteLock(lock_path, is_singleton=False).read_lock():
        # SQLite keeps its own descriptor, and the one that refused a symlink stays open with it: closing it would drop
        # this process's locks on the inode.
        assert descriptors_on(lock_path.stat()) == 2


@NEEDS_FD_DIRECTORY  # pragma: needs fd-directory
def test_read_write_lock_cycling_beside_a_holder_keeps_descriptors_flat(tmp_path: Path) -> None:
    lock_path: Final[Path] = tmp_path / "held.db"
    cycler: Final[ReadWriteLock] = ReadWriteLock(lock_path, is_singleton=False)
    with ReadWriteLock(lock_path, is_singleton=False).read_lock():
        with cycler.read_lock():
            pass
        before: Final[int] = descriptors_on(lock_path.stat())
        for _ in range(20):
            with cycler.read_lock():
                pass
        assert descriptors_on(lock_path.stat()) == before


@NEEDS_FD_DIRECTORY
def test_read_write_locks_held_together_each_keep_their_own_probe(tmp_path: Path) -> None:  # pragma: needs fd-directory
    # macOS SQLite fails, now and then, to open two live connections through one /dev/fd name.
    lock_path: Final[Path] = tmp_path / "shared.db"
    with (
        ReadWriteLock(lock_path, is_singleton=False).read_lock(),
        ReadWriteLock(lock_path, is_singleton=False).read_lock(),
    ):
        assert descriptors_on(lock_path.stat()) == 4


@NEEDS_FD_DIRECTORY
@pytest.mark.parametrize(
    ("first_name", "last_name"),
    [pytest.param("older", "newer", id="newer-last"), pytest.param("newer", "older", id="older-last")],
)
@pytest.mark.parametrize("next_database", [pytest.param("shared.db", id="same"), pytest.param("other.db", id="other")])
def test_read_write_lock_close_interrupted_among_its_probes_closes_the_rest_later(  # pragma: needs fd-directory
    tmp_path: Path,
    mocker: MockerFixture,
    first_name: Literal["older", "newer"],
    last_name: Literal["older", "newer"],
    next_database: str,
) -> None:
    lock_path: Final[Path] = tmp_path / "shared.db"
    older: Final[ReadWriteLock] = ReadWriteLock(lock_path, is_singleton=False)
    newer: Final[ReadWriteLock] = ReadWriteLock(lock_path, is_singleton=False)
    older.acquire_read()
    newer.acquire_read()
    locks: Final[dict[str, ReadWriteLock]] = {"older": older, "newer": newer}
    locks[first_name].release()
    real_close = os.close

    def close_then_interrupt(fd: int) -> None:
        real_close(fd)
        # A signal handler runs once the first probe's close returns, before the second one closes.
        mocker.stopall()
        raise KeyboardInterrupt

    mocker.patch("os.close", autospec=True, side_effect=close_then_interrupt)
    with pytest.raises(KeyboardInterrupt):
        locks[last_name].release()
    # The next connect must not adopt the probe the interrupted close already shut, and any connect finishes the close.
    with ReadWriteLock(tmp_path / next_database, is_singleton=False).read_lock():
        pass

    assert descriptors_on(lock_path.stat()) == 0


@_NEEDS_PROBE_DESCRIPTORS  # pragma: win32 no cover
@pytest.mark.parametrize(
    ("contender_output", "peer_count"),
    [pytest.param("", 0, id="alone"), pytest.param("Timeout\n", 1, id="beside-a-peer")],
)
def test_read_write_lock_release_interrupted_once_its_probe_release_applies(
    tmp_path: Path, mocker: MockerFixture, contender_output: str, *, peer_count: int
) -> None:
    lock_path: Final[Path] = tmp_path / "held.db"
    lock: Final[ReadWriteLock] = ReadWriteLock(lock_path, is_singleton=False)
    peers: Final[list[ReadWriteLock]] = [ReadWriteLock(lock_path, is_singleton=False) for _ in range(peer_count)]
    lock.acquire_read()
    for peer in peers:
        peer.acquire_read()
    release_locked = filelock._sqlite._connection._ProbeDescriptors._release_locked

    # A signal handler runs once the release returns, before the drain dequeues it.
    def apply_then_interrupt(
        descriptors: filelock._sqlite._connection._ProbeDescriptors,
        probe: filelock._sqlite._connection._Probe,
    ) -> NoReturn:
        release_locked(descriptors, probe)
        raise KeyboardInterrupt

    mocker.patch.object(
        filelock._sqlite._connection._ProbeDescriptors,
        "_release_locked",
        autospec=True,
        side_effect=apply_then_interrupt,
    )
    with pytest.raises(KeyboardInterrupt):
        lock.release()
    mocker.stopall()
    # Any connect drains the release the interrupt left queued, applying it again.
    with ReadWriteLock(tmp_path / "other.db", is_singleton=False).read_lock():
        pass

    # Closing any descriptor on the inode would drop the read lock the peer holds for this process.
    assert run_fork_script(_TRY_WRITE_SCRIPT, [str(lock_path)], timeout=30) == (0, contender_output, "")


@NEEDS_FD_DIRECTORY
@pytest.mark.parametrize(
    ("taking", "idle_connections"),
    [pytest.param("retain", 0, id="new-probe"), pytest.param("adopt", 1, id="idle-probe")],
)
def test_read_write_lock_connect_interrupted_once_it_leases_a_probe_releases_it(  # pragma: needs fd-directory
    tmp_path: Path, mocker: MockerFixture, taking: Literal["retain", "adopt"], idle_connections: int
) -> None:
    lock_path: Final[Path] = tmp_path / "held.db"
    holder: Final[ReadWriteLock] = ReadWriteLock(lock_path, is_singleton=False)
    holder.acquire_read()
    # A connection that closes beside a live one leaves its probe idle for the next connect to adopt.
    for _ in range(idle_connections):
        with ReadWriteLock(lock_path, is_singleton=False).read_lock():
            pass
    real = getattr(filelock._sqlite._connection._ProbeDescriptors, taking)

    def lease_then_interrupt(
        descriptors: filelock._sqlite._connection._ProbeDescriptors,
        fd_or_database: int | str,
        claimed: list[filelock._sqlite._connection._Probe],
    ) -> NoReturn:
        real(descriptors, fd_or_database, claimed)
        raise KeyboardInterrupt

    mocker.patch.object(
        filelock._sqlite._connection._ProbeDescriptors, taking, autospec=True, side_effect=lease_then_interrupt
    )
    with pytest.raises(KeyboardInterrupt):
        ReadWriteLock(lock_path, is_singleton=False).acquire_read()
    mocker.stopall()
    holder.release()

    assert descriptors_on(lock_path.stat()) == 0


@NEEDS_COLLECTED_FINALIZATION
@_NEEDS_PROBE_DESCRIPTORS
def test_read_write_lock_finalized_while_probes_are_recorded_does_not_deadlock(  # pragma: win32 no cover
    tmp_path: Path, mocker: MockerFixture
) -> None:
    # The collector runs on whichever allocation trips it, here the one made while the probe table is locked.
    real_probes = filelock._sqlite._connection._InodeProbes

    def collect_then_build() -> filelock._sqlite._connection._InodeProbes:
        gc.collect()
        return real_probes()

    gc.disable()
    try:
        cycle = SimpleNamespace(lock=ReadWriteLock(tmp_path / "dropped.db", is_singleton=False))
        cycle.lock.acquire_read()
        cycle.itself = cycle
        del cycle
        mocker.patch("filelock._sqlite._connection._InodeProbes", autospec=True, side_effect=collect_then_build)
        ReadWriteLock(tmp_path / "other.db", is_singleton=False).close()
    finally:
        gc.enable()
    assert writer_gets_in(tmp_path / "dropped.db")


_TRY_WRITE_SCRIPT: Final[str] = textwrap.dedent(
    """
    import sys
    from filelock import ReadWriteLock, Timeout
    try:
        ReadWriteLock(sys.argv[1], is_singleton=False).acquire_write(timeout=0)
    except Timeout:
        print("Timeout")
    """
)
