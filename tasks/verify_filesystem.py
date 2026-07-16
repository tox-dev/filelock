"""Verify that filelock holds mutual exclusion on a target filesystem, and exit non-zero when it does not.

Point it at a directory on the filesystem under test (an NFS or SMB mount, say) and it runs a mutual-exclusion check:
many processes each take the lock repeatedly and record the wall-clock interval they spend holding it. A correct lock
never lets two holders overlap, so the recorded intervals never intersect. Run with
``python tasks/verify_filesystem.py [directory]``.

Overlap detection rather than a shared counter is deliberate. A lost-update counter conflates lock exclusion with data
cache coherence: two independent NFS client caches can lose an update to a counter even under a perfectly exclusive
lock, because a read-modify-write reads a stale cached copy. Each process here only returns its own intervals (no shared
mutable state read across caches), so the check measures the lock and nothing else. CLOCK_MONOTONIC is system-wide on
one host, so intervals from sibling processes are directly comparable.

Every lock type is always run and printed, so the output records the real behavior of each on the filesystem.
FILELOCK_VERIFY_LOCKS narrows only which lock types failing turns the exit code non-zero: a filesystem where a type is
known-unsupported (native ``flock`` across NFS clients, the strict claim over SMB) still reports its result without
failing the gate.
"""

from __future__ import annotations

import os
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Final

from filelock import FileLock, SoftFileLock, StrictSoftFileLock

_PROCESSES: Final[int] = 8
_HOLDS: Final[int] = 200
_ACQUIRE_TIMEOUT: Final[float] = 45.0
_HOLD_SECONDS: Final[float] = 0.002
#: A pass needs this many holds completed, so exclusion is not affirmed vacuously when contenders were shut out.
_MIN_HELD: Final[int] = _PROCESSES * _HOLDS // 4
_ALL_LOCKS: Final[tuple[str, ...]] = ("FileLock", "SoftFileLock", "StrictSoftFileLock")


def main() -> int:
    # Each argument is a mount of the same filesystem. Two independent mounts (an NFS export mounted twice with
    # nosharecache, say) are two client caches over one server, so contending across them is a genuine multi-client
    # check, not two views of one cache. With no arguments a temporary directory checks the local single-mount case.
    if mounts := [Path(argument) for argument in sys.argv[1:]]:
        for mount in mounts:
            mount.mkdir(parents=True, exist_ok=True)
        return _verify_across(mounts)
    with TemporaryDirectory(prefix="filelock-verify-") as directory:
        return _verify_across([Path(directory)])


def _verify_across(mounts: list[Path]) -> int:
    where = str(mounts[0]) if len(mounts) == 1 else f"{len(mounts)} mounts of {mounts[0]} .. {mounts[-1]}"
    print(f"verifying mutual exclusion across {where} ({_PROCESSES} processes x {_HOLDS} holds)")
    gated = _gated_locks()
    failures = 0
    for name in _ALL_LOCKS:
        counts, overlaps, reason = _run_one(name, mounts)
        expected = _PROCESSES * _HOLDS
        # The guarantee under test is exclusion: no two holders overlap, over enough completed holds that the result is
        # not vacuous. Every other outcome the pathological loopback contention provokes is a failed acquire, which is
        # fail-closed and safe: a starvation Timeout, an EINVAL where CIFS rejects the strict claim, an ESTALE or EACCES
        # storm on NFSv3. held and min report throughput and fairness, which the filesystem drags down without ever
        # letting two holders in at once, so they are shown but do not gate.
        ok = overlaps == 0 and sum(counts) >= _MIN_HELD
        failures += (not ok) and name in gated
        note = "" if name in gated else " (ungated)"
        tail = f"{note}{f' {reason}' if reason else ''}"
        stats = f"held={sum(counts)}/{expected} min={min(counts)} overlaps={overlaps}"
        print(f"  {name:20} {'PASS' if ok else 'FAIL'}  {stats}{tail}")
    return 1 if failures else 0


def _gated_locks() -> frozenset[str]:
    if not (requested := os.environ.get("FILELOCK_VERIFY_LOCKS")):
        return frozenset(_ALL_LOCKS)
    return frozenset(requested.split(","))


def _run_one(name: str, mounts: list[Path]) -> tuple[list[int], int, str | None]:
    # Same basename on every mount, so the mounts contend on one server file through their independent caches.
    lock_paths = [str(mounts[index % len(mounts)] / f"{name}.lock") for index in range(_PROCESSES)]
    with ProcessPoolExecutor(max_workers=_PROCESSES) as pool:
        results = list(pool.map(_hammer, [name] * _PROCESSES, lock_paths))
    counts = [len(intervals) for intervals, _ in results]
    reason = next((reason for _, reason in results if reason is not None), None)
    return counts, _count_overlaps([intervals for intervals, _ in results]), reason


def _count_overlaps(per_process: list[list[tuple[float, float]]]) -> int:
    # Sweep the intervals in start order: a hold that begins before the latest end seen so far, and belongs to another
    # process, means two holders were inside the lock at once. A correct lock hands off strictly, so nothing overlaps.
    intervals = sorted((enter, leave, owner) for owner, held in enumerate(per_process) for enter, leave in held)
    overlaps = 0
    latest_leave = float("-inf")
    latest_owner = -1
    for enter, leave, owner in intervals:
        if enter < latest_leave and owner != latest_owner:
            overlaps += 1
        if leave > latest_leave:
            latest_leave, latest_owner = leave, owner
    return overlaps


def _hammer(name: str, lock_path: str) -> tuple[list[tuple[float, float]], str | None]:
    lock = _build(name, lock_path)
    intervals: list[tuple[float, float]] = []
    for _ in range(_HOLDS):
        try:
            lock.acquire(timeout=_ACQUIRE_TIMEOUT)
        except Exception as error:  # noqa: BLE001 - every acquire failure the filesystem provokes is fail-closed
            # A failed acquire never enters the critical section, so it cannot break exclusion. Record why this process
            # stopped (a starvation Timeout, EINVAL on CIFS, ESTALE or EACCES under NFSv3 churn) and end its run.
            return intervals, f"{type(error).__name__}: {error}".rstrip(": ")[:160]
        enter = time.monotonic()
        # Hold briefly so a broken lock lets a second holder in during an observable window; a correct lock serializes
        # the holds regardless. enter is stamped after acquire and leave before release, so a correct hand-off can
        # never look like an overlap even though release and the next acquire race.
        time.sleep(_HOLD_SECONDS)
        leave = time.monotonic()
        lock.release()
        intervals.append((enter, leave))
    return intervals, None


def _build(name: str, lock_path: str) -> FileLock | SoftFileLock | StrictSoftFileLock:
    if name == "FileLock":
        return FileLock(lock_path)
    if name == "SoftFileLock":
        return SoftFileLock(lock_path)
    return StrictSoftFileLock(lock_path)


if __name__ == "__main__":
    raise SystemExit(main())
