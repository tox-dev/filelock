from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from filelock import SoftFileLease, SoftFileLock, StrictSoftFileLock
from filelock._identity import _HOST_NAME_LIMIT, host_name

if TYPE_CHECKING:
    from pathlib import Path

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
    # Kernel hostnames that every marker format used to publish verbatim and then read back as malformed. The
    # surrogate is how Python surfaces a hostname that is not valid UTF-8.
    mocker.patch("filelock._identity.socket.gethostname", return_value=request.param)


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


def test_host_name_does_not_alias_two_hosts_onto_one(mocker: MockerFixture) -> None:
    # Two distinct hostnames past the field limit used to fold onto the same marker identity, so owner_is_stale
    # treated another machine's marker as its own and probed a local PID against it.
    shared = "a" * _HOST_NAME_LIMIT
    mocker.patch("filelock._identity.socket.gethostname", return_value=f"{shared}-alpha")
    alpha = host_name()
    mocker.patch("filelock._identity.socket.gethostname", return_value=f"{shared}-bravo")
    bravo = host_name()

    assert alpha != bravo
    assert len(alpha) <= _HOST_NAME_LIMIT
    assert len(bravo) <= _HOST_NAME_LIMIT


def test_host_name_keeps_an_in_limit_host_verbatim(mocker: MockerFixture) -> None:
    # The digest is only needed once something is dropped, so a name that fits must be unchanged or every
    # existing marker stops matching.
    mocker.patch("filelock._identity.socket.gethostname", return_value="wörks")
    assert host_name() == "w?c3?b6rks"

    mocker.patch("filelock._identity.socket.gethostname", return_value="a" * _HOST_NAME_LIMIT)
    assert host_name() == "a" * _HOST_NAME_LIMIT
