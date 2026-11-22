from __future__ import annotations

from contextlib import closing
from typing import TYPE_CHECKING, Final

import pytest

from filelock import ReadWriteLock, SoftReadWriteLock
from tests.sqlite.process_support import grant_everyone_write, holding_reader

if TYPE_CHECKING:
    import subprocess
    from collections.abc import Generator
    from pathlib import Path


@pytest.fixture
def posix_link_holder(shared_database: Path) -> Generator[subprocess.Popen[str]]:  # pragma: needs posix-hard-link
    with holding_reader(shared_database) as holder:
        yield holder


@pytest.fixture
def windows_link_holder(everyone_writable_database: Path) -> Generator[subprocess.Popen[str]]:  # pragma: win32 cover
    with holding_reader(everyone_writable_database) as holder:
        yield holder


@pytest.fixture
def shared_database(tmp_path: Path) -> Path:  # pragma: needs posix-hard-link
    # The shared parent forces SQLite to use a private link.
    shared: Final[Path] = tmp_path / "shared"
    shared.mkdir()
    shared.chmod(0o777)
    return shared / "lock.db"


@pytest.fixture
def lock_file(tmp_path: Path) -> str:
    return str(tmp_path / "test_lock.db")


@pytest.fixture
def everyone_writable_database(tmp_path: Path) -> Path:  # pragma: win32 cover
    shared = tmp_path / "shared"
    shared.mkdir()
    grant_everyone_write(shared)
    return shared / "lock.db"


@pytest.fixture
def clear_sqlite_singletons() -> Generator[None]:
    ReadWriteLock._instances.clear()
    yield
    for instance in list(ReadWriteLock._instances.valuerefs()):
        if (lock := instance()) is not None:  # pragma: no cover  # cache is normally emptied before teardown
            lock.close()
    ReadWriteLock._instances.clear()


@pytest.fixture(
    params=[
        pytest.param(ReadWriteLock, id="sqlite"),
        pytest.param(SoftReadWriteLock, id="soft", marks=pytest.mark.requires_hard_links),
    ]
)
def lock_type(request: pytest.FixtureRequest) -> type[ReadWriteLock | SoftReadWriteLock]:
    return request.param


@pytest.fixture
def sync_locks(tmp_path: Path) -> Generator[tuple[ReadWriteLock, ReadWriteLock]]:
    with (
        closing(ReadWriteLock(tmp_path / "a", timeout=30, is_singleton=False)) as holder,
        closing(ReadWriteLock(tmp_path / "a", timeout=30, is_singleton=False)) as contender,
    ):
        yield holder, contender
