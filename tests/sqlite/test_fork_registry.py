from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, Final

import pytest

pytest.importorskip("sqlite3")
from pathlib import Path

from filelock import ReadWriteLock
from filelock._sqlite._fork import (
    FORKED_DATABASES,
    _ForkedDatabaseRegistry,
    _in_subinterpreter,
    _track_sqlite_use,
)

if TYPE_CHECKING:
    from pytest_mock import MockerFixture


pytestmark: Final[pytest.MarkDecorator] = pytest.mark.usefixtures("clear_sqlite_singletons")


def test_subinterpreter_refusal_precedes_database_access(tmp_path: Path, mocker: MockerFixture) -> None:
    path = tmp_path / "lock.db"
    mocker.patch("filelock._sqlite._fork._IN_POSIX_SUBINTERPRETER", new=True)
    with pytest.raises(RuntimeError, match="main interpreter"):
        ReadWriteLock(path)
    assert not path.exists()


@pytest.mark.parametrize("main", [True, False])
def test_thread_interpreter_identity(mocker: MockerFixture, *, main: bool) -> None:
    mocker.patch("filelock._sqlite._fork._thread", new=SimpleNamespace(_is_main_interpreter=lambda: main))
    assert _in_subinterpreter() is not main


@pytest.mark.parametrize("current", [0, 1])
def test_legacy_interpreter_identity(mocker: MockerFixture, current: int) -> None:
    mocker.patch("filelock._sqlite._fork._thread", new=SimpleNamespace())
    mocker.patch(
        "filelock._sqlite._fork.import_module",
        autospec=True,
        return_value=SimpleNamespace(get_current=lambda: current, get_main=lambda: 0),
    )
    assert _in_subinterpreter() is (current != 0)


@pytest.mark.parametrize("implementation", ["cpython", "pypy"])
def test_runtime_without_interpreter_ids(mocker: MockerFixture, implementation: str) -> None:
    mocker.patch("filelock._sqlite._fork._thread", new=SimpleNamespace())
    mocker.patch("filelock._sqlite._fork.sys", new=SimpleNamespace(implementation=SimpleNamespace(name=implementation)))
    mocker.patch("filelock._sqlite._fork.import_module", autospec=True, side_effect=ModuleNotFoundError)
    assert not _in_subinterpreter()


def test_embedded_cpython_without_the_interpreters_module(mocker: MockerFixture) -> None:
    mocker.patch("filelock._sqlite._fork._thread", new=SimpleNamespace())
    mocker.patch("filelock._sqlite._fork.sys", new=SimpleNamespace(implementation=SimpleNamespace(name="cpython")))
    api = object()
    states = {("PyInterpreterState_Get", api): lambda: 7, ("PyInterpreterState_Main", api): lambda: 4}
    library = SimpleNamespace(PYFUNCTYPE=lambda _result: states.__getitem__, c_void_p=int, pythonapi=api)
    mocker.patch("filelock._sqlite._fork.import_module", autospec=True, side_effect=[ModuleNotFoundError, library])
    assert _in_subinterpreter()


def test_note_sqlite_use_records_use_on_pypy(mocker: MockerFixture) -> None:
    mocker.patch("filelock._sqlite._fork._IS_PYPY", True)
    registry = _ForkedDatabaseRegistry()
    registry.note_sqlite_use()
    assert registry._sqlite_used is True


@pytest.mark.parametrize("identity", [pytest.param(None, id="no-identity"), pytest.param((1, 2), id="with-identity")])
def test_poison_after_fork_records_path_and_identity(identity: tuple[int, int] | None) -> None:
    registry = _ForkedDatabaseRegistry()
    path = Path("poisoned.db")
    registry.poison_after_fork(path, identity)
    assert path in registry._paths
    assert (identity in registry._identities) is (identity is not None)


def test_track_sqlite_use_forwards_connect_event(mocker: MockerFixture) -> None:
    note = mocker.patch.object(FORKED_DATABASES, "note_sqlite_use", autospec=True)
    _track_sqlite_use("sqlite3.connect", ())
    note.assert_called_once_with()


def test_track_sqlite_use_ignores_other_events(mocker: MockerFixture) -> None:
    note = mocker.patch.object(FORKED_DATABASES, "note_sqlite_use", autospec=True)
    _track_sqlite_use("open", ())
    note.assert_not_called()
