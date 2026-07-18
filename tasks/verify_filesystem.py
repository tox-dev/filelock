"""Verify that filelock holds mutual exclusion on a target filesystem, and exit non-zero when it does not.

Point it at a directory on the filesystem under test (an NFS or SMB mount, say) and it runs a mutual-exclusion check:
several processes each take the lock a fixed number of times and record the wall-clock interval they spend holding it.
A correct lock never lets two holders overlap and lets every process finish its holds, so intervals never intersect and
the completed count matches the expected total. Run with ``python tasks/verify_filesystem.py [directory]``.

Overlap detection rather than a shared counter is deliberate. A lost-update counter conflates lock exclusion with data
cache coherence: two independent NFS client caches can lose an update to a counter even under a perfectly exclusive
lock, because a read-modify-write reads a stale cached copy. Each process here only returns its own intervals (no shared
mutable state read across caches), so the check measures the lock and nothing else. CLOCK_MONOTONIC is system-wide on
one host, so intervals from sibling processes are directly comparable.

The contention is deliberately moderate and dispersed so the result is deterministic: a small process count, a short
hold, and a randomized gap between holds keep any contender from being starved out, and the genuinely transient NFS
errors (a stale handle or a momentary permission race under concurrent create and unlink, which robust NFS clients
retry) are retried rather than treated as failures. A correct lock then finishes every hold on every run.

Every lock type is always run and printed, so the output records the real behavior of each on the filesystem.
FILELOCK_VERIFY_LOCKS narrows only which lock types failing turns the exit code non-zero: a filesystem where a type is
known-unsupported (the strict claim over SMB) still reports its result without failing the gate.
"""

from __future__ import annotations

import os
import random
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from errno import EACCES, ENOENT, ESTALE
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING, Final

from filelock import FileLock, SoftFileLock, StrictSoftFileLock

if TYPE_CHECKING:
    from collections.abc import Callable

_PROCESSES: Final[int] = 4
_HOLDS: Final[int] = 100
_ACQUIRE_TIMEOUT: Final[float] = 60.0
_HOLD_SECONDS: Final[float] = 0.004
_GAP_MAX_SECONDS: Final[float] = 0.008
_TRANSIENT_RETRIES: Final[int] = 16
_RETRY_BACKOFF: Final[float] = 0.01
_TRANSIENT_ERRNOS: Final[frozenset[int]] = frozenset({EACCES, ENOENT, ESTALE})
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
        # A correct lock never overlaps, finishes every hold, and raises no error the retries could not clear.
        ok = overlaps == 0 and sum(counts) == expected and reason is None
        failures += (not ok) and name in gated
        note = "" if name in gated else " (ungated)"
        tail = f"{note}{f' {reason}' if reason else ''}"
        stats = f"held={sum(counts)}/{expected} overlaps={overlaps}"
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
        if (reason := _resiliently(lambda: lock.acquire(timeout=_ACQUIRE_TIMEOUT))) is not None:
            return intervals, reason
        enter = time.monotonic()
        # Hold briefly so a broken lock lets a second holder in during an observable window; a correct lock serializes
        # the holds regardless. enter is stamped after acquire and leave before release, so a correct hand-off can never
        # look like an overlap even though release and the next acquire race.
        time.sleep(_HOLD_SECONDS)
        leave = time.monotonic()
        if (reason := _resiliently(lock.release)) is not None:
            intervals.append((enter, leave))
            return intervals, reason
        intervals.append((enter, leave))
        # A randomized gap outside the lock breaks the lock-step herd, so a poll-based lock hands off fairly and no
        # contender is starved out of finishing its holds.
        time.sleep(random.uniform(0, _GAP_MAX_SECONDS))  # ruff:ignore[suspicious-non-cryptographic-random-usage] - test dispersion, not cryptographic
    return intervals, None


def _resiliently(action: Callable[[], object]) -> str | None:
    # Retry the genuinely transient NFS errors (a stale handle, a create/unlink permission race under contention) that
    # a robust client retries, so one hiccup never aborts a hold. A Timeout from starvation or a rejection the
    # filesystem always makes (EINVAL for the strict claim on CIFS) is not transient and ends this process's run.
    for attempt in range(_TRANSIENT_RETRIES):
        if (error := _attempt(action)) is None:
            return None
        if not _has_transient(error) or attempt == _TRANSIENT_RETRIES - 1:
            return _describe(error)
        time.sleep(_RETRY_BACKOFF * (attempt + 1))
    return "transient errors exhausted retries"


def _attempt(action: Callable[[], object]) -> BaseException | None:
    try:
        action()
    except Exception as error:  # ruff:ignore[blind-except] - the caller classifies transient versus terminal; a harness must not die
        return error
    return None


def _has_transient(error: BaseException) -> bool:
    # An ExceptionGroup (the strict claim wraps its cleanup failures in one) exposes leaves via ``exceptions``; recurse
    # rather than name BaseExceptionGroup, which is not a builtin on the 3.10 floor this script is linted against.
    if (leaves := getattr(error, "exceptions", None)) is not None:
        return any(_has_transient(leaf) for leaf in leaves)
    return isinstance(error, OSError) and not isinstance(error, TimeoutError) and error.errno in _TRANSIENT_ERRNOS


def _describe(error: BaseException) -> str:
    return f"{type(error).__name__}: {error}"[:160]


def _build(name: str, lock_path: str) -> FileLock | SoftFileLock | StrictSoftFileLock:
    if name == "FileLock":
        return FileLock(lock_path)
    if name == "SoftFileLock":
        return SoftFileLock(lock_path)
    return StrictSoftFileLock(lock_path)


if __name__ == "__main__":
    raise SystemExit(main())
