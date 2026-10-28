from __future__ import annotations

import os
import pickle  # ruff:ignore[suspicious-pickle-import]  # round-trips classes this package defines
import subprocess  # ruff:ignore[suspicious-subprocess-import]  # a clean interpreter shows what the import itself loads
import sys
from textwrap import dedent
from typing import TYPE_CHECKING

import pytest
from capabilities import CAPABILITIES

import filelock

if TYPE_CHECKING:
    from pathlib import Path
    from typing import Final


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


def _hidden_module_script(blocked: str, missing: str, attribute: str) -> str:
    return f"""
        import sys
        class Hide:
            def find_spec(self, name, path=None, target=None):
                if name == {blocked!r}:
                    raise ModuleNotFoundError(name={missing!r})
        sys.meta_path.insert(0, Hide())
        import filelock
        try:
            value = getattr(filelock, {attribute!r})
        except ImportError as error:
            print(f"error:{{error.name}}")
        else:
            print("none" if value is None else "resolved")
        """


@pytest.mark.parametrize(
    ("blocked", "missing", "attribute"),
    [
        pytest.param("sqlite3", "sqlite3", "ReadWriteLock", id="package"),
        pytest.param("_sqlite3", "_sqlite3", "AsyncReadWriteLock", id="cpython-binding"),
        pytest.param("_sqlite3", "_sqlite3_cffi", "ReadWriteLock", id="pypy-binding"),
    ],
)
def test_lazy_import_missing_sqlite3_leaves_read_write_lock_none(blocked: str, missing: str, attribute: str) -> None:
    # coverage loads sqlite3 before filelock, so only an unmeasured child can miss it
    assert _run(_hidden_module_script(blocked, missing, attribute), measured=False) == "none\n"


@pytest.mark.parametrize(
    ("module", "attribute"),
    [
        pytest.param("filelock._read_write", "ReadWriteLock", id="sync-read-write"),
        pytest.param("filelock._async_read_write", "AsyncReadWriteLock", id="async-read-write"),
        pytest.param("filelock._soft", "SoftFileLock", id="other-lazy-module"),
    ],
)
def test_lazy_import_missing_filelock_module_raises(module: str, attribute: str) -> None:
    assert _run(_hidden_module_script(module, module, attribute)) == f"error:{module}\n"


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


@pytest.mark.skipif(sys.version_info < (3, 15), reason="lazy imports start with Python 3.15")
def test_lazy_import_missing_runtime_type_does_not_raise_at_exit() -> None:  # pragma: >=3.15 cover
    assert not subprocess.run(
        [
            sys.executable,
            "-c",
            "import types; types.__dict__.pop('LazyImportType', None); import filelock; filelock.FileLock('a')",
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stderr


_DAEMON_LOOP: Final = (
    "loop = asyncio.new_event_loop(); threading.Thread(target=loop.run_forever, daemon=True).start()\n"
)


@pytest.mark.skipif(not CAPABILITIES["lazy-imports"], reason="needs PEP 810 lazy imports")
@pytest.mark.parametrize(
    "lazy_imports",
    [
        pytest.param("normal", id="normal"),
        # pipx and other applications switch every import in the process to lazy
        pytest.param("all", id="all"),
    ],
)
@pytest.mark.parametrize(
    "hold",
    [
        pytest.param("lock = filelock.FileLock('a'); lock.acquire()", id="FileLock"),
        pytest.param("lock = filelock.SoftFileLock('a'); lock.acquire()", id="SoftFileLock"),
        pytest.param("lock = filelock.StrictSoftFileLock('a'); lock.acquire()", id="Strict"),
        pytest.param("lock = filelock.ReadWriteLock('a'); lock.acquire_write()", id="ReadWriteLock"),
        pytest.param(
            "loop = asyncio.new_event_loop(); lock = filelock.AsyncFileLock('a', loop=loop)\n"
            "loop.run_until_complete(lock.acquire())",
            id="AsyncFileLock",
        ),
        pytest.param(
            "loop = asyncio.new_event_loop(); lock = filelock.AsyncSoftFileLock('a', loop=loop)\n"
            "loop.run_until_complete(lock.acquire())",
            id="AsyncSoftFileLock",
        ),
        pytest.param(
            f"{_DAEMON_LOOP}lock = filelock.AsyncSoftFileLock('a', loop=loop, thread_local=False, "
            "run_in_executor=True)\nasyncio.run_coroutine_threadsafe(lock.acquire(), loop).result()",
            id="AsyncSoftFileLock-daemon-loop-shared",
        ),
        pytest.param(
            f"{_DAEMON_LOOP}lock = filelock.AsyncSoftFileLock('a', loop=loop, thread_local=True, "
            "run_in_executor=False)\nasyncio.run_coroutine_threadsafe(lock.acquire(), loop).result()",
            id="AsyncSoftFileLock-daemon-loop-thread-local",
        ),
        pytest.param(
            "loop = asyncio.new_event_loop(); lock = filelock.AsyncReadWriteLock('a')\n"
            "loop.run_until_complete(lock.acquire_write())",
            id="AsyncReadWriteLock",
        ),
    ],
)
def test_lazy_import_lock_held_at_exit_releases_as_eager(
    tmp_path: Path, hold: str, lazy_imports: str
) -> None:  # pragma: needs lazy-imports
    def held_at_exit(name: str, before_exit: str) -> tuple[list[str], list[str]]:
        (folder := tmp_path / name).mkdir()
        script = (
            f"import sys, types\nsys.set_lazy_imports({lazy_imports!r})\nimport asyncio, threading, filelock\n{hold}"
        )
        stderr = subprocess.run(
            [sys.executable, "-W", "ignore", "-c", f"{script}\n{before_exit}"],
            cwd=folder,
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        ).stderr
        # Under "all" the standard library's own finalizers (asyncio's proactor loop on Windows) can fail at shutdown
        # the same way; only frames from this package count against it.
        frames = [line for line in stderr.splitlines() if f"{os.sep}filelock{os.sep}" in line]
        return sorted(entry.name for entry in folder.iterdir()), frames

    # Resolving every lazy filelock binding before exit gives the eager behavior on this platform to compare against;
    # a filter that forces imports eager is no baseline, as installing one changes what a held lock does at exit.
    eager = held_at_exit(
        "eager",
        "for module in [m for n, m in list(sys.modules.items()) if n.partition('.')[0] == 'filelock']:\n"
        "    for attribute, value in list(vars(module).items()):\n"
        "        if type(value) is types.LazyImportType: getattr(module, attribute)",
    )

    assert held_at_exit("lazy", "") == (eager[0], [])
