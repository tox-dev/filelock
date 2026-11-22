from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from typing import TYPE_CHECKING, Any, Final

import pytest

from filelock import LeaseCompromise, SoftReadWriteLock, Timeout
from tests.capability_marks import (
    NEEDS_SYMLINK,
)

from .lock_helpers import make_lock, read_state

if TYPE_CHECKING:
    from pathlib import Path

pytestmark: Final[list[pytest.MarkDecorator]] = [
    pytest.mark.requires_hard_links,
    pytest.mark.usefixtures("clear_singletons"),
]


def test_singleton_returns_same_instance(lock_file: str) -> None:
    first = SoftReadWriteLock(lock_file)
    second = SoftReadWriteLock(lock_file)
    try:
        assert first is second
    finally:
        first.close()


@pytest.mark.parametrize(
    "order",
    [pytest.param((0, 1, 2), id="012"), pytest.param((1, 2, 0), id="120"), pytest.param((2, 0, 1), id="201")],
)
def test_singletons_are_distinct_per_subclass(lock_file: str, order: tuple[int, int, int]) -> None:
    class FirstLock(SoftReadWriteLock):
        pass

    class SecondLock(SoftReadWriteLock):
        pass

    lock_types = (SoftReadWriteLock, FirstLock, SecondLock)
    instances: list[SoftReadWriteLock] = []
    try:
        for index in order:
            lock_type = lock_types[index]
            instance = lock_type(lock_file)
            instances.append(instance)
            assert type(instance) is lock_type
            assert lock_type.get_lock(lock_file) is instance
        assert len({id(instance) for instance in instances}) == len(lock_types)
        with instances[0].write_lock():
            for contender in instances[1:]:
                with pytest.raises(Timeout):
                    contender.acquire_write(blocking=False)
    finally:
        for instance in instances:
            instance.close()


@pytest.mark.parametrize("relation", ["base", "sibling", "child"])
def test_singleton_construction_can_overlap_across_classes(lock_file: str, relation: str) -> None:
    entered, release = threading.Event(), threading.Event()

    class SlowLock(SoftReadWriteLock):
        def __init__(self, *args: Any, **kwargs: Any) -> None:  # ruff:ignore[any-type]  # forwards the public constructor unchanged
            if type(self) is SlowLock:
                entered.set()
                assert release.wait(timeout=5)
            super().__init__(*args, **kwargs)

    class SiblingLock(SoftReadWriteLock):
        pass

    class ChildLock(SlowLock):
        pass

    other_type = {"base": SoftReadWriteLock, "sibling": SiblingLock, "child": ChildLock}[relation]
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(SlowLock, lock_file)
        try:
            assert entered.wait(timeout=5)
            with closing(pool.submit(other_type, lock_file).result(timeout=5)) as other:
                assert type(other) is other_type
                assert other_type.get_lock(lock_file) is other
        finally:
            release.set()
            with closing(first.result(timeout=5)) as instance:
                assert type(instance) is SlowLock
                assert SlowLock.get_lock(lock_file) is instance


@pytest.mark.parametrize("is_singleton", [True, False])
def test_subclass_forwards_cooperative_class_keywords(lock_file: str, is_singleton: bool) -> None:
    class LabelMixin:
        label: str

        def __init_subclass__(cls, *, label: str, **kwargs: object) -> None:
            super().__init_subclass__(**kwargs)
            cls.label = label

    class LabeledLock(SoftReadWriteLock, LabelMixin, label="invoice"):
        pass

    class ChildLock(LabeledLock, label="child"):
        pass

    with (
        closing(LabeledLock(lock_file, is_singleton=is_singleton)) as parent,
        closing(ChildLock(lock_file, is_singleton=is_singleton)) as child,
    ):
        assert type(parent) is LabeledLock
        assert type(child) is ChildLock
        assert (parent.label, child.label) == ("invoice", "child")


def test_subclass_rejects_recursive_singleton_construction_and_can_retry(lock_file: str) -> None:
    recurse = True

    class RecursiveLock(SoftReadWriteLock):
        def __init__(self, *args: Any, **kwargs: Any) -> None:  # ruff:ignore[any-type]  # forwards the public constructor unchanged
            if recurse:
                RecursiveLock(lock_file)
            super().__init__(*args, **kwargs)

    with pytest.raises(RuntimeError, match="Singleton lock construction is already active"):
        RecursiveLock(lock_file)
    recurse = False
    with closing(RecursiveLock(lock_file)) as instance:
        assert RecursiveLock(lock_file) is instance


def test_subclass_concurrent_constructors_share_one_singleton(lock_file: str) -> None:
    callers = threading.Barrier(8)
    init_calls = 0

    class CustomLock(SoftReadWriteLock):
        def __init__(self, *args: Any, **kwargs: Any) -> None:  # ruff:ignore[any-type]  # forwards the public constructor unchanged
            nonlocal init_calls
            init_calls += 1
            super().__init__(*args, **kwargs)

    def construct() -> SoftReadWriteLock:
        callers.wait(timeout=5)
        return CustomLock(lock_file)

    with ThreadPoolExecutor(max_workers=8) as pool:
        instances = list(pool.map(lambda _: construct(), range(8)))
    try:
        assert init_calls == 1
        assert all(instance is instances[0] for instance in instances)
    finally:
        for instance in instances:
            instance.close()


def test_non_singleton_returns_distinct_instances(lock_file: str) -> None:
    first = SoftReadWriteLock(lock_file, is_singleton=False)
    second = SoftReadWriteLock(lock_file, is_singleton=False)
    try:
        assert first is not second
    finally:
        first.close()
        second.close()


def test_singleton_mismatch_raises(lock_file: str) -> None:
    first = SoftReadWriteLock(lock_file, timeout=5)
    try:
        with pytest.raises(ValueError, match="cannot be changed"):
            SoftReadWriteLock(lock_file, timeout=10)
    finally:
        first.close()


def test_get_lock_returns_singleton(lock_file: str) -> None:
    first = SoftReadWriteLock.get_lock(lock_file)
    second = SoftReadWriteLock.get_lock(lock_file)
    try:
        assert first is second
    finally:
        first.close()


def test_leaked_acquired_singleton_is_closed_on_teardown(lock_file: str) -> None:
    # The heartbeat keeps the weakly cached singleton reachable until teardown.
    SoftReadWriteLock(lock_file, heartbeat_interval=0.5).acquire_write(timeout=2)
    assert read_state(lock_file).writer is not None


@NEEDS_SYMLINK
def test_a_symlinked_lock_path_keeps_its_log_beside_the_symlink(tmp_path: Path) -> None:  # pragma: needs symlink
    # Older peers locate the log beside the spelled path.
    (alias := tmp_path / "alias.lock").symlink_to(tmp_path / "real.lock")
    lock = make_lock(str(alias))
    try:
        with lock.write_lock(timeout=2):
            assert sorted(entry.name for entry in tmp_path.iterdir()) == ["alias.lock", "alias.lock.rw"]
    finally:
        lock.close()


@NEEDS_SYMLINK
def test_a_symlink_and_its_target_are_separate_singletons(tmp_path: Path) -> None:  # pragma: needs symlink
    (alias := tmp_path / "alias.lock").symlink_to(target := tmp_path / "real.lock")
    via_alias = SoftReadWriteLock(alias)
    via_target = SoftReadWriteLock(target)
    try:
        assert via_alias is not via_target
    finally:
        via_alias.close()
        via_target.close()


def test_singleton_rejects_a_different_on_compromise(lock_file: str) -> None:
    seen: list[LeaseCompromise] = []
    first = SoftReadWriteLock(lock_file, on_compromise=seen.append)
    try:
        assert SoftReadWriteLock(lock_file) is first
        assert SoftReadWriteLock(lock_file, on_compromise=seen.append) is first
        with pytest.raises(ValueError, match="different on_compromise"):
            SoftReadWriteLock(lock_file, on_compromise=seen.remove)
    finally:
        first.close()
