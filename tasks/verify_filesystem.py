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
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else None
    if target is not None:
        target.mkdir(parents=True, exist_ok=True)
        return _verify_in(target)
    with TemporaryDirectory(prefix="filelock-verify-") as directory:
        return _verify_in(Path(directory))


def _selected_locks() -> tuple[str, ...]:
    # FILELOCK_VERIFY_LOCKS narrows the set to a comma-separated subset, so a filesystem that supports only some lock
    # types (SMB has no atomic no-replace hard link for the strict claim protocol) is checked for the ones it does.
    if not (requested := os.environ.get("FILELOCK_VERIFY_LOCKS")):
        return _ALL_LOCKS
    return tuple(name for name in _ALL_LOCKS if name in requested.split(","))


def _verify_in(directory: Path) -> int:
    print(f"verifying mutual exclusion in {directory} ({_PROCESSES} processes x {_INCREMENTS} increments)")
    failures = 0
    for name in _selected_locks():
        counter = directory / f"{name}.counter"
        counter.write_text("0", encoding="utf-8")
        lock_path = str(directory / f"{name}.lock")
        with ProcessPoolExecutor(max_workers=_PROCESSES) as pool:
            list(pool.map(_hammer, [name] * _PROCESSES, [lock_path] * _PROCESSES, [str(counter)] * _PROCESSES))
        total = int(counter.read_text(encoding="utf-8"))
        expected = _PROCESSES * _INCREMENTS
        ok = total == expected
        failures += not ok
        print(f"  {name:20} {'PASS' if ok else 'FAIL'}  counter={total} expected={expected}")
    return 1 if failures else 0


def _hammer(name: str, lock_path: str, counter_path: str) -> None:
    lock = _build(name, lock_path)
    for _ in range(_INCREMENTS):
        with lock:
            current = int(Path(counter_path).read_text(encoding="utf-8"))
            Path(counter_path).write_text(str(current + 1), encoding="utf-8")


def _build(name: str, lock_path: str) -> FileLock | SoftFileLock | StrictSoftFileLock:
    if name == "FileLock":
        return FileLock(lock_path)
    if name == "SoftFileLock":
        return SoftFileLock(lock_path)
    return StrictSoftFileLock(lock_path)


if __name__ == "__main__":
    raise SystemExit(main())
