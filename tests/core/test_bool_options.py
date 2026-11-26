from __future__ import annotations

from typing import TYPE_CHECKING, Final

import pytest

from filelock import (
    AsyncFileLock,
    FileLock,
    ReadWriteLock,
    SoftFileLock,
    SoftReadWriteLock,
)

if TYPE_CHECKING:
    from pathlib import Path


_NON_BOOL: Final[list[object]] = [
    pytest.param("false", id="str"),
    pytest.param(0, id="zero"),
    pytest.param(1, id="one"),
    pytest.param(None, id="none"),
]


@pytest.mark.parametrize("value", _NON_BOOL)
@pytest.mark.parametrize("option", ["thread_local", "is_singleton", "fallback_to_soft", "preserve_lock_file"])
@pytest.mark.parametrize("lock_type", [FileLock, SoftFileLock])
def test_constructor_bool_option_rejects_non_bool(
    lock_type: type[FileLock], option: str, value: str | int | None, tmp_path: Path
) -> None:
    before = dict(lock_type._instances)
    with pytest.raises(TypeError, match=rf"^{option} must be a bool, not "):
        lock_type(tmp_path / "a.lock", **{option: value})  # ty: ignore[invalid-argument-type]  # the rejected type is the contract
    assert dict(lock_type._instances) == before


@pytest.mark.parametrize("value", _NON_BOOL)
def test_sqlite_is_singleton_rejects_non_bool(value: str | int | None, tmp_path: Path) -> None:
    before = dict(ReadWriteLock._instances)
    with pytest.raises(TypeError, match=r"^is_singleton must be a bool, not "):
        ReadWriteLock(tmp_path / "a.db", is_singleton=value)  # ty: ignore[invalid-argument-type]  # the rejected type is the contract
    assert dict(ReadWriteLock._instances) == before


@pytest.mark.requires_hard_links
@pytest.mark.parametrize("value", _NON_BOOL)
def test_soft_read_write_is_singleton_rejects_non_bool(value: str | int | None, tmp_path: Path) -> None:
    before = dict(SoftReadWriteLock._instances)
    with pytest.raises(TypeError, match=r"^is_singleton must be a bool, not "):
        SoftReadWriteLock(tmp_path / "a.lock", is_singleton=value)  # ty: ignore[invalid-argument-type]  # the rejected type is the contract
    assert dict(SoftReadWriteLock._instances) == before


@pytest.mark.parametrize("value", _NON_BOOL)
def test_async_run_in_executor_rejects_non_bool(value: str | int | None, tmp_path: Path) -> None:
    before = dict(AsyncFileLock._instances)
    with pytest.raises(TypeError, match=r"^run_in_executor must be a bool, not "):
        AsyncFileLock(tmp_path / "a.lock", run_in_executor=value)  # ty: ignore[invalid-argument-type]  # the rejected type is the contract
    assert dict(AsyncFileLock._instances) == before
