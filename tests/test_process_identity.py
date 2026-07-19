from __future__ import annotations

import os
import socket
import sys
from errno import ENODEV, EPERM
from typing import TYPE_CHECKING, Final

import pytest

from filelock._identity import host_name, owner_is_stale, process_alive, process_start_token

if TYPE_CHECKING:
    from pytest_mock import MockerFixture

_DEAD_PID: Final[int] = 2**22 + 1
_POSIX_ONLY: Final[pytest.MarkDecorator] = pytest.mark.skipif(sys.platform == "win32", reason="posix kill semantics")


def test_host_name_matches_socket() -> None:
    assert host_name() == socket.gethostname()


def test_process_alive_true_for_self() -> None:
    assert process_alive(os.getpid()) is True


def test_process_alive_false_for_dead() -> None:
    assert process_alive(_DEAD_PID) is False


@_POSIX_ONLY
def test_process_alive_reads_permission_denied_as_alive(mocker: MockerFixture) -> None:  # pragma: win32 no cover
    mocker.patch("filelock._identity.os.kill", side_effect=OSError(EPERM, "operation not permitted"))
    assert process_alive(_DEAD_PID) is True


@_POSIX_ONLY
def test_process_alive_reraises_unexpected_errno(mocker: MockerFixture) -> None:  # pragma: win32 no cover
    mocker.patch("filelock._identity.os.kill", side_effect=OSError(ENODEV, "no such device"))
    with pytest.raises(OSError, match="no such device"):  # pragma: win32 no cover
        process_alive(_DEAD_PID)


def test_process_start_token_is_int_for_self() -> None:
    assert isinstance(process_start_token(os.getpid()), int)


def test_process_start_token_none_for_dead() -> None:
    assert process_start_token(_DEAD_PID) is None


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS sysctl probe")
def test_darwin_sysctl_probe_failure_reads_no_token(mocker: MockerFixture) -> None:  # pragma: darwin cover
    mocker.patch("filelock._identity._LIBC.sysctl", return_value=-1)
    assert process_start_token(os.getpid()) is None


def test_owner_is_stale_foreign_host_never_reclaimed() -> None:
    assert owner_is_stale(os.getpid(), "another-host.example.com", 1) is False


def test_owner_is_stale_dead_process_reclaimed() -> None:
    assert owner_is_stale(_DEAD_PID, host_name(), 1) is True


def test_owner_is_stale_live_process_without_token_held() -> None:
    assert owner_is_stale(os.getpid(), host_name(), None) is False


def test_owner_is_stale_live_process_matching_token_held() -> None:
    assert owner_is_stale(os.getpid(), host_name(), process_start_token(os.getpid())) is False


def test_owner_is_stale_live_process_mismatched_token_reclaimed() -> None:
    token = process_start_token(os.getpid())
    assert token is not None
    assert owner_is_stale(os.getpid(), host_name(), token + 1) is True


def test_owner_is_stale_live_process_unreadable_token_held(mocker: MockerFixture) -> None:
    # A live PID whose current start token cannot be read is indistinguishable from the recorded owner, so it holds.
    mocker.patch("filelock._identity.process_start_token", return_value=None)
    assert owner_is_stale(os.getpid(), host_name(), 987654) is False
