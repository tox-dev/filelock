from __future__ import annotations

import asyncio
import gc
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING, Literal, Protocol, TypedDict, cast
from weakref import ref

import pytest

from filelock import BaseAsyncFileLock, BaseFileLock, CloseErrorPolicy, ContextErrorPolicy
from tests.capability_marks import NEEDS_CLASS_COLLECTION

if TYPE_CHECKING:
    from collections.abc import Callable
    from concurrent.futures import Executor
    from pathlib import Path

    from typing_extensions import Unpack


@pytest.mark.parametrize(
    ("option", "value"),
    [
        pytest.param("timeout", 1, id="timeout"),
        pytest.param("mode", 0o600, id="mode"),
        pytest.param("thread_local", False, id="thread-local"),
        pytest.param("blocking", False, id="blocking"),
        pytest.param("is_singleton", True, id="singleton"),
        pytest.param("poll_interval", 0.1, id="poll-interval"),
        pytest.param("lifetime", 1, id="lifetime"),
        pytest.param("context_error_policy", "group", id="context-error-policy"),
        pytest.param("close_error_policy", "raise", id="close-error-policy"),
        pytest.param("fallback_to_soft", False, id="fallback-to-soft"),
        pytest.param("preserve_lock_file", True, id="preserve-lock-file"),
        pytest.param("on_acquired", list[int]().append, id="on-acquired"),
    ],
)
def test_narrow_sync_subclass_rejects_non_default_option(
    option: str,
    value: float | str | Callable[[int], None],
    tmp_path: Path,
) -> None:
    with pytest.raises(TypeError, match=option):
        _sync_constructor()(
            str(tmp_path / "lock"),
            **cast("_FileLockOptions", {option: value}),
        )


def test_narrow_sync_subclass_rejects_unknown_option(tmp_path: Path) -> None:
    with pytest.raises(TypeError, match="unknown"):
        _unknown_constructor()(str(tmp_path / "lock"), unknown=True)


@pytest.mark.parametrize(
    "option",
    [
        pytest.param("thread_local", id="thread-local"),
        pytest.param("loop", id="loop"),
        pytest.param("run_in_executor", id="run-in-executor"),
        pytest.param("executor", id="executor"),
    ],
)
def test_narrow_async_subclass_rejects_non_default_option(
    option: Literal["thread_local", "loop", "run_in_executor", "executor"],
    tmp_path: Path,
) -> None:
    loop = asyncio.new_event_loop()
    try:
        with ThreadPoolExecutor(max_workers=1) as executor, pytest.raises(TypeError, match=option):
            _async_constructor()(
                str(tmp_path / "lock"),
                **_unsupported_async_options(option, loop, executor),
            )
    finally:
        loop.close()


@pytest.mark.parametrize(
    "kind",
    [pytest.param("sync", id="sync"), pytest.param("async", id="async")],
)
def test_narrow_subclass_accepts_explicit_base_defaults(
    kind: Literal["sync", "async"],
    tmp_path: Path,
) -> None:
    lock_path = str(tmp_path / "lock")
    assert _build_with_explicit_defaults(kind, lock_path).lock_file == lock_path


def test_narrow_subclass_accepts_named_option(tmp_path: Path) -> None:
    assert _NamedFileLock(str(tmp_path / "lock"), marker=7).marker == 7


def test_sync_subclass_forwards_kwargs(tmp_path: Path) -> None:
    hook: Callable[[int], None] = list[int]().append
    lock = _ForwardingFileLock(
        str(tmp_path / "lock"),
        timeout=2,
        mode=0o600,
        thread_local=False,
        blocking=False,
        is_singleton=True,
        poll_interval=0.1,
        lifetime=3,
        context_error_policy="group",
        close_error_policy="suppress",
        fallback_to_soft=False,
        preserve_lock_file=True,
        on_acquired=hook,
    )

    assert {
        "timeout": lock.timeout,
        "mode": lock.mode,
        "thread_local": lock.is_thread_local(),
        "blocking": lock.blocking,
        "is_singleton": lock.is_singleton,
        "poll_interval": lock.poll_interval,
        "lifetime": lock.lifetime,
        "context_error_policy": lock.context_error_policy,
        "close_error_policy": lock.close_error_policy,
        "fallback_to_soft": lock.fallback_to_soft,
        "preserve_lock_file": lock.preserve_lock_file,
        "on_acquired": lock.on_acquired,
    } == {
        "timeout": 2,
        "mode": 0o600,
        "thread_local": False,
        "blocking": False,
        "is_singleton": True,
        "poll_interval": 0.1,
        "lifetime": 3,
        "context_error_policy": "group",
        "close_error_policy": "suppress",
        "fallback_to_soft": False,
        "preserve_lock_file": True,
        "on_acquired": hook,
    }


def test_async_subclass_forwards_kwargs(tmp_path: Path) -> None:
    hook: Callable[[int], None] = list[int]().append
    loop = asyncio.new_event_loop()
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            lock = _ForwardingAsyncFileLock(
                str(tmp_path / "lock"),
                timeout=2,
                mode=0o600,
                thread_local=True,
                blocking=False,
                is_singleton=True,
                poll_interval=0.1,
                lifetime=3,
                context_error_policy="group",
                close_error_policy="suppress",
                fallback_to_soft=False,
                preserve_lock_file=True,
                on_acquired=hook,
                loop=loop,
                run_in_executor=False,
                executor=executor,
            )

            assert {
                "timeout": lock.timeout,
                "mode": lock.mode,
                "thread_local": lock.is_thread_local(),
                "blocking": lock.blocking,
                "is_singleton": lock.is_singleton,
                "poll_interval": lock.poll_interval,
                "lifetime": lock.lifetime,
                "context_error_policy": lock.context_error_policy,
                "close_error_policy": lock.close_error_policy,
                "fallback_to_soft": lock.fallback_to_soft,
                "preserve_lock_file": lock.preserve_lock_file,
                "on_acquired": lock.on_acquired,
                "loop": lock.loop,
                "run_in_executor": lock.run_in_executor,
                "executor": lock.executor,
            } == {
                "timeout": 2,
                "mode": 0o600,
                "thread_local": True,
                "blocking": False,
                "is_singleton": True,
                "poll_interval": 0.1,
                "lifetime": 3,
                "context_error_policy": "group",
                "close_error_policy": "suppress",
                "fallback_to_soft": False,
                "preserve_lock_file": True,
                "on_acquired": hook,
                "loop": loop,
                "run_in_executor": False,
                "executor": executor,
            }
    finally:
        loop.close()


@pytest.mark.parametrize(
    "option",
    [pytest.param("hook", id="hook"), pytest.param("policy", id="policy")],
)
def test_forwarding_subclass_singleton_rejects_changed_option(
    option: Literal["hook", "policy"],
    tmp_path: Path,
) -> None:
    hook: Callable[[int], None] = list[int]().append
    lock_path = str(tmp_path / "lock")
    first = _ForwardingFileLock(lock_path, is_singleton=True, on_acquired=hook)

    with pytest.raises(ValueError, match="on_acquired" if option == "hook" else "context_error_policy"):
        _change_singleton_option(option, lock_path, hook)
    assert first.on_acquired is hook


@NEEDS_CLASS_COLLECTION
def test_constructor_model_does_not_retain_dynamic_subclass(tmp_path: Path) -> None:
    lock_type = _dynamic_constructor()
    class_ref = ref(lock_type)
    lock_type(str(tmp_path / "lock"))
    del lock_type
    gc.collect()
    assert class_ref() is None


def _sync_constructor() -> type[BaseFileLock]:
    return cast("type[BaseFileLock]", _NarrowFileLock)


def _async_constructor() -> type[BaseAsyncFileLock]:
    return cast("type[BaseAsyncFileLock]", _NarrowAsyncFileLock)


def _unknown_constructor() -> _UnknownFileLockConstructor:
    return cast("_UnknownFileLockConstructor", _NarrowFileLock)


def _unsupported_async_options(
    option: Literal["thread_local", "loop", "run_in_executor", "executor"],
    loop: asyncio.AbstractEventLoop,
    executor: Executor,
) -> _AsyncFileLockOptions:
    if option == "thread_local":
        return {"thread_local": True, "run_in_executor": False}
    if option == "loop":
        return {"loop": loop}
    if option == "run_in_executor":
        return {"run_in_executor": False}
    return {"executor": executor}


def _build_with_explicit_defaults(kind: Literal["sync", "async"], lock_path: str) -> BaseFileLock:
    if kind == "sync":
        return _sync_constructor()(
            lock_path,
            timeout=-1,
            mode=-1,
            thread_local=True,
            blocking=True,
            is_singleton=False,
            poll_interval=0.05,
            lifetime=None,
            context_error_policy="chain",
            close_error_policy="default",
            fallback_to_soft=True,
            preserve_lock_file=False,
            on_acquired=None,
        )
    return _async_constructor()(
        lock_path,
        timeout=-1,
        mode=-1,
        thread_local=False,
        blocking=True,
        is_singleton=False,
        poll_interval=0.05,
        lifetime=None,
        context_error_policy="chain",
        close_error_policy="default",
        fallback_to_soft=True,
        preserve_lock_file=False,
        on_acquired=None,
        loop=None,
        run_in_executor=True,
        executor=None,
    )


def _change_singleton_option(
    option: Literal["hook", "policy"],
    lock_path: str,
    hook: Callable[[int], None],
) -> BaseFileLock:
    if option == "hook":
        return _ForwardingFileLock(lock_path, is_singleton=True, on_acquired=list[int]().append)
    return _ForwardingFileLock(
        lock_path,
        is_singleton=True,
        context_error_policy="group",
        on_acquired=hook,
    )


def _dynamic_constructor() -> type[BaseFileLock]:
    class DynamicFileLock(BaseFileLock):
        def _acquire(self) -> None:
            raise NotImplementedError

        def _release(self) -> None:
            raise NotImplementedError

    return DynamicFileLock


class _NarrowFileLock(BaseFileLock):
    _lifetime_supported = True

    def __init__(self, lock_file: str) -> None:
        super().__init__(lock_file)

    def _acquire(self) -> None:
        raise NotImplementedError

    def _release(self) -> None:
        raise NotImplementedError


class _NamedFileLock(BaseFileLock):
    def __init__(self, lock_file: str, *, marker: int = 0) -> None:
        super().__init__(lock_file)
        self.marker = marker

    def _acquire(self) -> None:
        raise NotImplementedError

    def _release(self) -> None:
        raise NotImplementedError


class _NarrowAsyncFileLock(BaseAsyncFileLock):
    _lifetime_supported = True

    def __init__(self, lock_file: str) -> None:
        super().__init__(lock_file)

    def _acquire(self) -> None:
        raise NotImplementedError

    def _release(self) -> None:
        raise NotImplementedError


class _ForwardingFileLock(BaseFileLock):
    _lifetime_supported = True

    def __init__(self, lock_file: str, **kwargs: Unpack[_FileLockOptions]) -> None:
        super().__init__(lock_file, **kwargs)

    def _acquire(self) -> None:
        raise NotImplementedError

    def _release(self) -> None:
        raise NotImplementedError


class _ForwardingAsyncFileLock(BaseAsyncFileLock):
    _lifetime_supported = True

    def __init__(self, lock_file: str, **kwargs: Unpack[_AsyncFileLockOptions]) -> None:
        super().__init__(lock_file, **kwargs)

    def _acquire(self) -> None:
        raise NotImplementedError

    def _release(self) -> None:
        raise NotImplementedError


class _FileLockOptions(TypedDict, total=False):
    timeout: float
    mode: int
    thread_local: bool
    blocking: bool
    is_singleton: bool
    poll_interval: float
    lifetime: float | None
    context_error_policy: ContextErrorPolicy
    close_error_policy: CloseErrorPolicy
    fallback_to_soft: bool
    preserve_lock_file: bool
    on_acquired: Callable[[int], None] | None


class _AsyncFileLockOptions(_FileLockOptions, total=False):
    loop: asyncio.AbstractEventLoop | None
    run_in_executor: bool
    executor: Executor | None


class _UnknownFileLockConstructor(Protocol):
    def __call__(self, lock_file: str, *, unknown: bool) -> BaseFileLock: ...
