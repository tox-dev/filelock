from __future__ import annotations

import gc
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from collections.abc import Generator


@pytest.fixture(autouse=True, scope="session")
def _force_gc_before_shutdown() -> Generator[None]:
    yield
    # Force GC so ReadWriteLock.__del__ closes SQLite connections while the interpreter is
    # still healthy.  Without this PyPy segfaults in Cursor.__del__._reset during shutdown.
    gc.collect()
