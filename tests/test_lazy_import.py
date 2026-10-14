from __future__ import annotations

import os
import pickle  # ruff:ignore[suspicious-pickle-import]  # round-trips classes this package defines
import subprocess  # ruff:ignore[suspicious-subprocess-import]  # a clean interpreter shows what the import itself loads
import sys
from textwrap import dedent

import pytest

import filelock


def _run(script: str, *, measured: bool = True) -> str:
    # A measured child has asyncio and sqlite3 loaded by coverage before filelock, hiding what the import itself loads.
    env = os.environ if measured else {k: v for k, v in os.environ.items() if k != "COVERAGE_PROCESS_CONFIG"}
    return subprocess.run(
        [sys.executable, "-c", dedent(script)], check=True, capture_output=True, text=True, env=env
    ).stdout


def test_lazy_import_skips_asyncio_and_sqlite3() -> None:
    script = """
        import sys
        import filelock
        print(sorted({"asyncio", "sqlite3"} & sys.modules.keys()))
        """

    assert _run(script, measured=False) == "[]\n"


@pytest.mark.parametrize(
    "script",
    [
        pytest.param(
            """
            import filelock
            print([name for name in filelock.__all__ if getattr(filelock, name) is None])
            """,
            id="attribute",
        ),
        pytest.param(
            """
            import filelock
            namespace = {}
            for name in filelock.__all__:
                exec(f"from filelock import {name}", namespace)
            print([name for name in filelock.__all__ if namespace[name] is None])
            """,
            id="from-import",
        ),
    ],
)
def test_lazy_import_resolves_every_public_name(script: str) -> None:
    assert _run(script) == "[]\n"


def test_lazy_import_resolves_asyncio_submodule() -> None:
    assert _run("from filelock import asyncio; print(asyncio.__name__)") == "filelock.asyncio\n"


def test_lazy_import_async_file_lock_subclasses_file_lock() -> None:
    assert {filelock.FileLock, filelock.BaseAsyncFileLock} <= set(filelock.AsyncFileLock.__mro__)


def test_lazy_import_class_pickles_by_reference() -> None:
    payload = pickle.dumps(filelock.AsyncFileLock)
    assert pickle.loads(payload) is filelock.AsyncFileLock  # ruff:ignore[suspicious-pickle-usage]  # payload made here


def test_lazy_import_dir_lists_every_public_name() -> None:
    assert _run("import filelock; print(sorted(set(filelock.__all__) - set(dir(filelock))))") == "[]\n"


def test_lazy_import_unknown_name_raises() -> None:
    with pytest.raises(AttributeError, match="module 'filelock' has no attribute 'missing'"):
        _ = filelock.missing
