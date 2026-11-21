from __future__ import annotations

import time
from contextlib import closing
from pathlib import Path
from typing import TYPE_CHECKING, Final, Literal

from filelock import LeaseCompromise, SoftReadWriteLock, Timeout
from filelock._soft._read_write._generations import GenerationLog, Snapshot
from filelock._soft._read_write._storage import OsFiles, encode_holder, new_token

if TYPE_CHECKING:
    from collections.abc import Callable
    from multiprocessing.synchronize import Event as EventType


# Slow process startup on Windows needs a longer deadline than a contention probe.
PROCESS_DEADLINE: Final[int] = 30


def read_state(lock_file: str) -> Snapshot:
    return GenerationLog(OsFiles(lock_file), lock_file, f"{lock_file}.rw").latest()


def writer_gets_in(lock_file: str) -> bool:
    with closing(make_lock(lock_file)) as contender:
        try:
            contender.acquire_write(blocking=False)
        except Timeout:
            return False
    return True


def make_lock(
    path: str,
    *,
    heartbeat_interval: float = 0.1,
    stale_threshold: float = 0.5,
    poll_interval: float = 0.02,
    is_singleton: bool = False,
    on_compromise: Callable[[LeaseCompromise], None] | None = None,
) -> SoftReadWriteLock:
    return SoftReadWriteLock(
        path,
        heartbeat_interval=heartbeat_interval,
        stale_threshold=stale_threshold,
        poll_interval=poll_interval,
        is_singleton=is_singleton,
        on_compromise=on_compromise,
    )


def holders(lock_file: str) -> list[str]:
    directory = Path(f"{lock_file}.rw", "holders")
    return sorted(entry.name for entry in directory.iterdir()) if directory.is_dir() else []


def worker(
    lock_file: str,
    mode: Literal["read", "write"],
    acquired_event: EventType,
    release_event: EventType | None = None,
    timeout: float = -1,
    blocking: bool = True,
    heartbeat_interval: float = 0.1,
    stale_threshold: float = 1.0,
    poll_interval: float = 0.02,
) -> None:
    lock = SoftReadWriteLock(
        lock_file,
        timeout=timeout,
        blocking=blocking,
        is_singleton=False,
        heartbeat_interval=heartbeat_interval,
        stale_threshold=stale_threshold,
        poll_interval=poll_interval,
    )
    try:
        with lock.read_lock() if mode == "read" else lock.write_lock():
            acquired_event.set()
            if release_event is not None:
                release_event.wait(timeout=PROCESS_DEADLINE)
    finally:
        lock.close()


def wait_until(condition: Callable[[], bool]) -> None:
    deadline = time.monotonic() + PROCESS_DEADLINE
    while not condition():
        assert time.monotonic() < deadline
        time.sleep(0.01)  # simplint: ignore[sleep-in-test]  # polls on-disk state; needs a library hook


def plant_holder(lock_file: str, *, mode: Literal["read", "write"], host: str = "terminated-pod") -> str:
    files = OsFiles(lock_file)
    root = f"{lock_file}.rw"
    files.prepare(root)
    token = new_token()
    record = encode_holder(token, new_token(), 0.5).replace(b"host=", f"host={host}?".encode("ascii"), 1)
    files.create(str(Path(root, "holders", token)), record)
    log = GenerationLog(files, lock_file, root)
    latest = log.latest()
    writer, readers = (token, latest.readers) if mode == "write" else (latest.writer, latest.readers | {token})
    assert log.commit(Snapshot(generation=latest.generation + 1, writer=writer, readers=readers))
    return token


def generations(lock_file: str) -> list[str]:
    return sorted(entry.name for entry in Path(f"{lock_file}.rw", "gen").iterdir() if entry.name.isdigit())


__all__ = [
    "PROCESS_DEADLINE",
    "generations",
    "holders",
    "make_lock",
    "plant_holder",
    "read_state",
    "wait_until",
    "worker",
    "writer_gets_in",
]
