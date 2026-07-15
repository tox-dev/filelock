"""Confirm filelock took its documented fallback when ``FILELOCK_BLOCK_MODULE`` hid a module, or exit non-zero.

The capability jobs set the module and put ``tasks/capability`` on ``PYTHONPATH`` so the sibling ``sitecustomize``
hides it; this checks the import-time consequence before the verifier exercises the resulting lock.
"""

from __future__ import annotations

import os

import filelock


def main() -> None:
    blocked = os.environ["FILELOCK_BLOCK_MODULE"]
    if blocked == "fcntl" and filelock.has_fcntl:
        msg = f"without fcntl, expected has_fcntl False, got {filelock.has_fcntl!r}"
        raise SystemExit(msg)
    if blocked == "sqlite3" and filelock.ReadWriteLock is not None:
        msg = f"without sqlite3, expected ReadWriteLock None, got {filelock.ReadWriteLock!r}"
        raise SystemExit(msg)
    print(f"fallback engaged without {blocked}")


if __name__ == "__main__":
    main()
