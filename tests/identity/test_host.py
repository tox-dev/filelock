from __future__ import annotations

import os
import sys
from errno import EACCES
from pathlib import Path
from typing import TYPE_CHECKING, Final

import pytest

from filelock import SoftFileLease, SoftFileLock, StrictSoftFileLock
from filelock._identity import describe_host_name, host_name
from tests.pid_namespace_helpers import INITIAL_PID_NAMESPACE, pin_pid_namespace

if TYPE_CHECKING:
    from pytest_mock import MockerFixture


@pytest.fixture(
    params=[
        pytest.param("host with space", id="space"),
        pytest.param("host\nname", id="newline"),
        pytest.param("wörks", id="non-ascii"),
        pytest.param("b\udcffd", id="undecodable-byte"),
    ]
)
def out_of_grammar_host(request: pytest.FixtureRequest, mocker: MockerFixture) -> None:
    # Python represents an invalid UTF-8 hostname byte with a surrogate.
    mocker.patch("socket.gethostname", return_value=request.param, autospec=True)


@pytest.mark.usefixtures("out_of_grammar_host")
def test_soft_file_lock_recognizes_its_own_marker(tmp_path: Path) -> None:
    with SoftFileLock(tmp_path / "resource.lock", timeout=2) as lock:
        assert lock.is_lock_held_by_us is True


@pytest.mark.requires_hard_links
@pytest.mark.usefixtures("out_of_grammar_host")
def test_strict_soft_file_lock_reads_back_its_own_claim(tmp_path: Path) -> None:  # pragma: needs hard-link
    with StrictSoftFileLock(tmp_path / "resource.lock", timeout=2) as lock:
        assert {claim.hostname for claim in lock.claims} == {host_name()}


@pytest.mark.usefixtures("out_of_grammar_host")
def test_soft_file_lease_reads_back_its_own_owner(tmp_path: Path) -> None:
    lease = SoftFileLease(str(tmp_path / "resource.lock"), timeout=2, lease_duration=30, heartbeat_interval=1)
    with lease:
        owner = lease.owner
        assert owner is not None
        assert owner.hostname == host_name()


@pytest.mark.usefixtures("out_of_grammar_host")
def test_describe_host_name_shows_this_hosts_name_unchanged() -> None:
    assert describe_host_name(host_name()) == host_name()


@pytest.mark.parametrize(
    ("recorded", "shown"),
    [
        pytest.param("build-host", "build-host", id="plain"),
        pytest.param("host?20name?pidns-f0000001", "host?20name?pidns-f0000001", id="already-escaped"),
        pytest.param("host\x1b[2K\x1b[1A\x7f\tname", "host?1b[2K?1b[1A?7f?09name", id="control-bytes"),
        pytest.param("w\u00f6rks", "w?c3?b6rks", id="non-ascii"),
        pytest.param("b\udcffd", "b?ffd", id="undecodable-byte"),
        pytest.param("", "?", id="empty"),
    ],
)
def test_describe_host_name_stays_within_the_marker_grammar(recorded: str, shown: str) -> None:
    assert describe_host_name(recorded) == shown


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        pytest.param("build-01.example.com", "build-01.example.com", id="plain"),
        pytest.param("build 01", "build?2001", id="space"),
        pytest.param("build\n01", "build?0a01", id="newline"),
        pytest.param("wörks", "w?c3?b6rks", id="non-ascii"),
        pytest.param("who?", "who?3f", id="escape-character"),
        pytest.param("b\udcffd", "b?ffd", id="undecodable-byte"),
        pytest.param("x" * 253, "x" * 253, id="at-limit"),
        pytest.param("x" * 300, f"{'x' * 244}-04c26261", id="over-long"),
        pytest.param("ä" * 200, f"{'?c3?a4' * 40}?c3?-de6b44df", id="over-long-escaped"),
        pytest.param("", "?", id="empty"),
    ],
)
def test_host_name_escapes_out_of_grammar_bytes(raw: str, expected: str, mocker: MockerFixture) -> None:
    pin_pid_namespace(mocker, INITIAL_PID_NAMESPACE)
    mocker.patch("socket.gethostname", return_value=raw, autospec=True)
    assert host_name() == expected


def test_host_name_keeps_over_long_names_apart_past_the_limit(mocker: MockerFixture) -> None:
    pin_pid_namespace(mocker, INITIAL_PID_NAMESPACE)
    mocker.patch(
        "socket.gethostname",
        side_effect=[f"{'a' * 253}-alpha", f"{'a' * 253}-bravo"],
        autospec=True,
    )
    alpha, bravo = host_name(), host_name()

    assert (alpha != bravo, len(alpha), len(bravo)) == (True, 253, 253)


@pytest.mark.skipif(sys.platform not in {"linux", "android"}, reason="Linux PID namespaces")  # pragma: linux cover
@pytest.mark.parametrize(
    ("namespace", "expected"),
    [
        pytest.param(INITIAL_PID_NAMESPACE, "build-01", id="initial"),
        pytest.param(0xF0000A1C, "build-01?pidns-f0000a1c", id="container"),
        pytest.param(PermissionError(EACCES, "Permission denied"), "build-01", id="unreadable"),
    ],
)
def test_host_name_names_the_pid_namespace(namespace: int | OSError, expected: str, mocker: MockerFixture) -> None:
    pin_pid_namespace(mocker, namespace)
    mocker.patch("socket.gethostname", return_value="build-01", autospec=True)
    assert host_name() == expected


@pytest.mark.skipif(sys.platform not in {"linux", "android"}, reason="Linux PID namespaces")  # pragma: linux cover
def test_host_name_keeps_a_namespace_apart_from_a_lookalike_host(mocker: MockerFixture) -> None:
    mocker.patch("socket.gethostname", side_effect=["a", "a?pidns-f0000001"], autospec=True)
    pin_pid_namespace(mocker, 0xF0000001)
    in_namespace: Final[str] = host_name()
    pin_pid_namespace(mocker, INITIAL_PID_NAMESPACE)
    assert in_namespace != host_name()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows has no PID namespaces")
def test_host_name_ignores_drive_root_proc_path(mocker: MockerFixture) -> None:  # pragma: win32 cover
    mocker.patch("socket.gethostname", return_value="build-01", autospec=True)
    mocker.patch.object(Path, "stat", autospec=True, return_value=os.stat_result((0, 0xF0000001) + (0,) * 8))
    assert host_name() == "build-01"
