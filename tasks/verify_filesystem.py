"""Verify that filelock holds mutual exclusion on a target filesystem, and exit non-zero when it does not.

Point it at a directory on the filesystem under test (an NFS or SMB mount, say) and it runs a lost-update check: many
processes each increment a shared counter under a lock, and a correct lock leaves the counter at exactly the expected
total. A lower total means two holders overlapped. Run with ``python tasks/verify_filesystem.py [directory]``.
"""

from __future__ import annotations

import os
import sys
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Final

from filelock import FileLock, SoftFileLock, StrictSoftFileLock

_PROCESSES: Final[int] = 8
_INCREMENTS: Final[int] = 200
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


def _selected_locks() -> tuple[str, ...]:
    # FILELOCK_VERIFY_LOCKS narrows the set to a comma-separated subset, so a filesystem that supports only some lock
    # types (SMB has no atomic no-replace hard link for the strict claim protocol) is checked for the ones it does.
    if not (requested := os.environ.get("FILELOCK_VERIFY_LOCKS")):
        return _ALL_LOCKS
    return tuple(name for name in _ALL_LOCKS if name in requested.split(","))


def _verify_across(mounts: list[Path]) -> int:
    where = str(mounts[0]) if len(mounts) == 1 else f"{len(mounts)} mounts of {mounts[0]} .. {mounts[-1]}"
    print(f"verifying mutual exclusion across {where} ({_PROCESSES} processes x {_INCREMENTS} increments)")
    failures = 0
    for name in _selected_locks():
        # Every process uses the same basename, so the mounts contend on one server file through independent caches.
        (mounts[0] / f"{name}.counter").write_text("0", encoding="utf-8")
        lock_paths = [str(mounts[index % len(mounts)] / f"{name}.lock") for index in range(_PROCESSES)]
        counter_paths = [str(mounts[index % len(mounts)] / f"{name}.counter") for index in range(_PROCESSES)]
        with ProcessPoolExecutor(max_workers=_PROCESSES) as pool:
            list(pool.map(_hammer, [name] * _PROCESSES, lock_paths, counter_paths))
        total = int((mounts[0] / f"{name}.counter").read_text(encoding="utf-8"))
        expected = _PROCESSES * _INCREMENTS
        ok = total == expected
        failures += not ok
        print(f"  {name:20} {'PASS' if ok else 'FAIL'}  counter={total} expected={expected}")
    return 1 if failures else 0


def _hammer(name: str, lock_path: str, counter_path: str) -> None:
    lock = _build(name, lock_path)
    counter = Path(counter_path)
    for _ in range(_INCREMENTS):
        with lock:
            current = int(counter.read_text(encoding="utf-8"))
            # Replace the counter atomically. A plain write truncates first, and a second client cache reading that
            # empty window would fail even though the lock held; a temp file renamed into place never appears empty,
            # so a lost update shows up as a short final count rather than a crash.
            temporary = counter.with_name(f"{counter.name}.{os.getpid()}.tmp")
            temporary.write_text(str(current + 1), encoding="utf-8")
            temporary.replace(counter)


def _build(name: str, lock_path: str) -> FileLock | SoftFileLock | StrictSoftFileLock:
    if name == "FileLock":
        return FileLock(lock_path)
    if name == "SoftFileLock":
        return SoftFileLock(lock_path)
    return StrictSoftFileLock(lock_path)


if __name__ == "__main__":
    raise SystemExit(main())
