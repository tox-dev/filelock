from __future__ import annotations

import functools
import os
from pathlib import Path
from typing import TYPE_CHECKING, Final

import pytest

from filelock import SoftFileLockProtocolError, StrictSoftFileLock

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from pytest_mock import MockerFixture

    from .filesystem_faults import Inspect, PathValue
from .capability_marks import WINDOWS_ONLY

pytestmark: Final[pytest.MarkDecorator] = pytest.mark.requires_hard_links


@WINDOWS_ONLY  # pragma: win32 cover
@pytest.mark.parametrize(
    ("operation", "match"),
    [
        pytest.param(lambda lock: lock.acquire(), "is not a real directory", id="acquire"),
        pytest.param(lambda lock: lock.claims, "cannot open claim directory", id="claims"),
    ],
)
def test_strict_soft_refuses_a_junction_for_the_claim_directory(
    tmp_path: Path,
    create_junction: Callable[[Path, Path], None],
    operation: Callable[[StrictSoftFileLock], object],
    match: str,
) -> None:
    lock_path: Final[Path] = tmp_path / "resource.lock"
    Path(f"{lock_path}.filelock").mkdir()
    (target := tmp_path / "target").mkdir()
    create_junction(Path(f"{lock_path}.filelock", "claims"), target)

    with pytest.raises(SoftFileLockProtocolError, match=match):
        operation(StrictSoftFileLock(lock_path, timeout=0))
    assert list(target.iterdir()) == []


@WINDOWS_ONLY  # pragma: win32 cover
def test_strict_soft_coordination_directory_swapped_for_a_junction_leaves_target_alone(
    tmp_path: Path, mocker: MockerFixture, create_junction: Callable[[Path, Path], None]
) -> None:
    lock_path: Final[Path] = tmp_path / "resource.lock"
    (coordination := Path(f"{lock_path}.filelock")).mkdir()
    (target := tmp_path / "target").mkdir()
    original_target = target.stat()
    swapped: list[bool] = []

    def swap_after_the_inspection(inspect: Inspect) -> Inspect:
        @functools.wraps(inspect)
        def inspect_then_swap(path: PathValue, **options: int | bool | None) -> os.stat_result:
            # The directory can become a junction after inspection.
            found = inspect(path, **options)
            if not swapped and os.fsdecode(path) == str(coordination):
                swapped.append(True)
                coordination.rename(tmp_path / "aside")
                create_junction(coordination, target)
            return found

        return inspect_then_swap

    # pathlib inspects the directory with os.lstat on 3.14 and with os.stat(follow_symlinks=False) before that.
    for function in ("lstat", "stat"):
        mocker.patch(
            f"os.{function}",
            side_effect=swap_after_the_inspection(getattr(os, function)),
            autospec=True,
        )

    with pytest.raises(SoftFileLockProtocolError, match="cannot open"):
        StrictSoftFileLock(lock_path, timeout=0).acquire()
    assert coordination.samefile(target)
    assert list(target.iterdir()) == []
    assert os.path.samestat(original_target, target.stat())


@WINDOWS_ONLY  # pragma: win32 cover
def test_strict_soft_keeps_the_claim_directory_in_place_while_it_scans(
    initialized_lock_path: Path, mocker: MockerFixture
) -> None:
    # Without dir_fd every operation resolves the claim directory by name, so nothing may rename it meanwhile.
    real_scandir = os.scandir
    aside: Final[Path] = initialized_lock_path.parent / "aside"
    refused: list[bool] = []

    def try_to_move_it_aside(path: int | str) -> Iterator[os.DirEntry[str]]:
        try:
            Path(f"{initialized_lock_path}.filelock").rename(aside)
        except PermissionError:
            refused.append(True)
        return real_scandir(path)

    mocker.patch("os.scandir", side_effect=try_to_move_it_aside, autospec=True)
    with StrictSoftFileLock(initialized_lock_path, timeout=0) as lock:
        assert lock.is_locked

    assert (bool(refused), aside.exists()) == (True, False)
