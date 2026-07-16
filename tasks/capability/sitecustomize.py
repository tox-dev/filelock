"""Hide the module named in ``FILELOCK_BLOCK_MODULE`` so a capability job can run filelock without it.

Put this directory on ``PYTHONPATH`` and set ``FILELOCK_BLOCK_MODULE=fcntl`` (or ``sqlite3``); every process, including
the workers a verifier spawns, then imports it and refuses that one module, so filelock takes its documented fallback.
A ``None`` entry in ``sys.modules`` is CPython's marker for "known unimportable", so it forces ``ImportError`` for a
module already cached at interpreter start (``fcntl`` is a builtin) as well as for a later first import (``sqlite3``).
"""

from __future__ import annotations

import os
import sys

if _blocked := os.environ.get("FILELOCK_BLOCK_MODULE"):
    sys.modules[_blocked] = None  # ty: ignore[invalid-assignment]  # a None entry is the documented unimportable marker
