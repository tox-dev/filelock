from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from filelock import ReadWriteLock, SoftReadWriteLock

if TYPE_CHECKING:
    from collections.abc import Generator


@pytest.fixture
def clear_rw_singletons() -> Generator[None]:
    caches = (ReadWriteLock._instances, SoftReadWriteLock._instances)
    for cache in caches:
        cache.clear()
    yield
    for cache in caches:
        for lock in filter(None, (ref() for ref in list(cache.valuerefs()))):
            lock.close()
        cache.clear()
