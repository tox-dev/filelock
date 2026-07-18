from __future__ import annotations

import os
import socket
import sys
from errno import EBADF, ENOSYS, EXDEV
from pathlib import Path
from typing import TYPE_CHECKING, Final

import pytest

if sys.version_info >= (3, 11):
    from builtins import ExceptionGroup
else:  # pragma: no cover (<py311)
    from exceptiongroup import ExceptionGroup

from filelock import (
    AsyncStrictSoftFileLock,
    SoftFileLock,
    SoftFileLockProtocolError,
    StrictSoftFileClaim,
    StrictSoftFileClaimState,
    StrictSoftFileLock,
    Timeout,
)
from filelock._identity import process_start_token

if TYPE_CHECKING:
    from pytest_mock import MockerFixture

_SENTINEL: Final[bytes] = b"1\nfilelock-strict-v1\x00\n0\n"


pytestmark = pytest.mark.requires_hard_links


def test_strict_soft_acquire_publishes_owner_claim(tmp_path: Path) -> None:
    lock_path = tmp_path / "resource.lock"
    lock = StrictSoftFileLock(lock_path)

    with lock:
        assert lock.claims == (
            StrictSoftFileClaim(
                name=lock.claims[0].name,
                state="held",
                token=lock.claims[0].token,
                pid=os.getpid(),
                hostname=socket.gethostname(),
                start=process_start_token(os.getpid()),
            ),
        )
    assert (lock_path.read_bytes(), lock.claims, lock.is_locked) == (_SENTINEL, (), False)


def test_strict_soft_is_reentrant(tmp_path: Path) -> None:
    lock = StrictSoftFileLock(tmp_path / "resource.lock")

    with lock:
        with lock:
            assert (lock.is_locked, lock.lock_counter, len(lock.claims)) == (True, 2, 1)
        assert (lock.is_locked, lock.lock_counter, len(lock.claims)) == (True, 1, 1)
    assert (lock.is_locked, lock.lock_counter, lock.claims) == (False, 0, ())


def test_strict_soft_contender_times_out(tmp_path: Path) -> None:
    lock_path = tmp_path / "resource.lock"
    holder = StrictSoftFileLock(lock_path)

    with holder, pytest.raises(Timeout):
        StrictSoftFileLock(lock_path, timeout=0).acquire()
    with StrictSoftFileLock(lock_path, timeout=0) as acquired:
        assert acquired.is_locked


def test_strict_soft_waits_for_legacy_holder(tmp_path: Path) -> None:
    lock_path = tmp_path / "resource.lock"
    legacy = SoftFileLock(lock_path)
    strict = StrictSoftFileLock(lock_path, timeout=0)

    with legacy, pytest.raises(Timeout):
        strict.acquire()
    with strict:
        assert strict.is_locked


def test_legacy_client_cannot_cross_permanent_sentinel(tmp_path: Path) -> None:
    lock_path = tmp_path / "resource.lock"
    with StrictSoftFileLock(lock_path):
        pass
    os.utime(lock_path, (0, 0))

    with pytest.raises(Timeout):
        SoftFileLock(lock_path, timeout=0).acquire()
    assert lock_path.read_bytes() == _SENTINEL


def test_legacy_client_never_reclaims_permanent_sentinel(tmp_path: Path, mocker: MockerFixture) -> None:
    lock_path = tmp_path / "resource.lock"
    lock_path.write_bytes(_SENTINEL)
    os.utime(lock_path, (0, 0))
    mocker.patch("filelock._identity.socket.gethostname", return_value="filelock-strict-v1\x00")
    mocker.patch("filelock._identity.os.kill", side_effect=ProcessLookupError)

    with pytest.raises(Timeout):
        SoftFileLock(lock_path, timeout=0).acquire()
    assert lock_path.read_bytes() == _SENTINEL


def test_strict_soft_rejects_mutable_descriptor_hook(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="on_acquired is not supported"):
        StrictSoftFileLock(tmp_path / "resource.lock", on_acquired=lambda _fd: None)


def test_strict_soft_preserves_sentinel_when_requested(tmp_path: Path) -> None:
    lock_path = tmp_path / "resource.lock"
    with StrictSoftFileLock(lock_path, preserve_lock_file=True):
        pass
    assert lock_path.read_bytes() == _SENTINEL


@pytest.mark.parametrize(
    "lock_type",
    [
        pytest.param(StrictSoftFileLock, id="sync"),
        pytest.param(AsyncStrictSoftFileLock, id="async"),
    ],
)
def test_strict_soft_constructor_rejects_lifetime(
    tmp_path: Path,
    lock_type: type[StrictSoftFileLock | AsyncStrictSoftFileLock],
) -> None:
    with pytest.warns(UserWarning, match=rf"lifetime is ignored for {lock_type.__name__}") as captured:
        lock = lock_type(tmp_path / "resource.lock", lifetime=1)

    assert (lock.lifetime, captured[0].filename) == (None, __file__)


@pytest.mark.parametrize(
    "lock_type",
    [
        pytest.param(StrictSoftFileLock, id="sync"),
        pytest.param(AsyncStrictSoftFileLock, id="async"),
    ],
)
def test_strict_soft_setter_rejects_lifetime(
    tmp_path: Path,
    lock_type: type[StrictSoftFileLock | AsyncStrictSoftFileLock],
) -> None:
    lock = lock_type(tmp_path / "resource.lock")

    with pytest.warns(UserWarning, match=rf"lifetime is ignored for {lock_type.__name__}") as captured:
        lock.lifetime = 1

    assert (lock.lifetime, captured[0].filename) == (None, __file__)


@pytest.mark.parametrize(
    "claim_name",
    [
        pytest.param("", id="empty"),
        pytest.param(".private.tmp", id="private"),
        pytest.param("../claim", id="parent"),
        pytest.param("directory/claim", id="slash"),
        pytest.param(r"directory\claim", id="backslash"),
        pytest.param("claim\0name", id="null"),
    ],
)
def test_strict_soft_force_break_rejects_non_basename(tmp_path: Path, claim_name: str) -> None:
    with pytest.raises(ValueError, match="one public claim basename"):
        StrictSoftFileLock(tmp_path / "resource.lock").force_break(claim_name)


def test_strict_soft_force_break_names_guarantee_loss(tmp_path: Path) -> None:
    lock_path = tmp_path / "resource.lock"
    holder = StrictSoftFileLock(lock_path)
    holder.acquire()
    claim_name = holder.claims[0].name

    StrictSoftFileLock(lock_path).force_break(claim_name)
    with StrictSoftFileLock(lock_path, timeout=0) as contender:
        assert (holder.is_locked, contender.is_locked) == (True, True)
    holder.release()


def test_strict_soft_force_break_requires_exact_case(tmp_path: Path) -> None:
    holder = StrictSoftFileLock(tmp_path / "resource.lock")
    holder.acquire()
    claim_name = holder.claims[0].name

    with pytest.raises(FileNotFoundError):
        StrictSoftFileLock(holder.lock_file).force_break(claim_name.upper())
    assert [claim.name for claim in holder.claims] == [claim_name]
    holder.release()


def test_strict_soft_force_break_accepts_newer_version_claim(tmp_path: Path) -> None:
    lock_path = tmp_path / "resource.lock"
    claims = Path(f"{lock_path}.filelock") / "claims"
    claims.mkdir(parents=True)
    claim_name = "held-v2-opaque.claim"
    (claims / claim_name).write_text("filelock-strict-v2\n", encoding="ascii", newline="")
    lock = StrictSoftFileLock(lock_path, timeout=0)

    with pytest.raises(SoftFileLockProtocolError, match="unknown claim name or protocol version"):
        lock.acquire()
    lock.force_break(claim_name)
    with lock:
        assert lock.is_locked


@pytest.mark.parametrize(
    ("claim_name", "content", "reason"),
    [
        pytest.param("held-v0-dead.claim", b"old\n", "unknown claim name or protocol version", id="old-version"),
        pytest.param("unrecognized", b"unknown\n", "unknown claim name or protocol version", id="unknown-name"),
        pytest.param(
            "held-v1-gggggggggggggggggggggggggggggggg.claim",
            b"unknown\n",
            "unknown claim name or protocol version",
            id="invalid-token",
        ),
        pytest.param(
            "held-v1-00000000000000000000000000000000.claim",
            b"truncated\n",
            "malformed claim record",
            id="malformed-record",
        ),
    ],
)
def test_strict_soft_invalid_claim_fails_closed(tmp_path: Path, claim_name: str, content: bytes, reason: str) -> None:
    lock_path = tmp_path / "resource.lock"
    claims = Path(f"{lock_path}.filelock") / "claims"
    claims.mkdir(parents=True)
    (claims / claim_name).write_bytes(content)
    lock = StrictSoftFileLock(lock_path, timeout=0)

    with pytest.raises(SoftFileLockProtocolError, match=reason) as raised:
        lock.acquire()
    assert (raised.value.lock_file, raised.value.claim_name, raised.value.reason, lock.is_locked) == (
        str(lock_path),
        claim_name,
        reason,
        False,
    )


@pytest.mark.parametrize("state", [pytest.param("intent", id="intent"), pytest.param("held", id="held")])
def test_strict_soft_orphan_claim_blocks_without_reclamation(tmp_path: Path, state: StrictSoftFileClaimState) -> None:
    lock_path = tmp_path / "resource.lock"
    token = "0" * 32
    claim_name = f"{state}-v1-{token}.claim"
    claims = Path(f"{lock_path}.filelock") / "claims"
    claims.mkdir(parents=True)
    (claims / claim_name).write_text(
        f"filelock-strict-v1\n{token}\n{os.getpid()}\n{socket.gethostname().encode().hex()}\n4242\n",
        encoding="ascii",
        newline="",
    )
    lock = StrictSoftFileLock(lock_path, timeout=0)

    with pytest.raises(Timeout):
        lock.acquire()
    assert lock.claims == (
        StrictSoftFileClaim(
            name=claim_name, state=state, token=token, pid=os.getpid(), hostname=socket.gethostname(), start=4242
        ),
    )


def test_strict_soft_rejects_non_directory_coordination_path(tmp_path: Path) -> None:
    lock_path = tmp_path / "resource.lock"
    Path(f"{lock_path}.filelock").write_text("not a directory", encoding="utf-8")

    with pytest.raises(SoftFileLockProtocolError, match="is not a real directory"):
        StrictSoftFileLock(lock_path).acquire()


@pytest.mark.skipif(sys.platform == "win32", reason="Unix permission bits control claim readability")
def test_strict_soft_unreadable_claim_fails_closed(tmp_path: Path) -> None:
    lock_path = tmp_path / "resource.lock"
    lock = StrictSoftFileLock(lock_path)
    lock.acquire()
    claim = Path(f"{lock_path}.filelock") / "claims" / lock.claims[0].name
    claim.chmod(0)
    try:
        with pytest.raises(SoftFileLockProtocolError, match="cannot read claim"):
            _ = StrictSoftFileLock(lock_path).claims
    finally:
        claim.chmod(0o600)
        lock.release()


def test_strict_soft_symlink_claim_fails_closed(tmp_path: Path) -> None:
    lock_path = tmp_path / "resource.lock"
    claims = Path(f"{lock_path}.filelock") / "claims"
    claims.mkdir(parents=True)
    target = tmp_path / "claim-target"
    target.write_bytes(b"filelock-strict-v1\n" + b"0" * 32 + b"\n1\n686f7374\n")
    claim = claims / f"held-v1-{'0' * 32}.claim"
    claim.symlink_to(target)

    with pytest.raises(SoftFileLockProtocolError, match="cannot read claim"):
        StrictSoftFileLock(lock_path).acquire()


@pytest.mark.parametrize(
    "error",
    [
        pytest.param(OSError(EXDEV, "cross-device link"), id="filesystem"),
        pytest.param(OSError(ENOSYS, "link unavailable"), id="kernel"),
        pytest.param(NotImplementedError("hard links unavailable"), id="runtime"),
    ],
)
def test_strict_soft_hard_link_failure_names_filesystem_contract(
    tmp_path: Path, mocker: MockerFixture, error: OSError | NotImplementedError
) -> None:
    mocker.patch("filelock._strict.os.link", side_effect=error)

    with pytest.raises(SoftFileLockProtocolError, match="atomic no-replace hard-link"):
        StrictSoftFileLock(tmp_path / "resource.lock").acquire()


def test_strict_soft_write_failure_leaves_no_public_claim(tmp_path: Path, mocker: MockerFixture) -> None:
    lock_path = tmp_path / "resource.lock"
    with StrictSoftFileLock(lock_path):
        pass
    mocker.patch("filelock._util.os.write", side_effect=OSError("write failed"))
    lock = StrictSoftFileLock(lock_path)

    with pytest.raises(OSError, match="write failed"):
        lock.acquire()
    assert (lock.claims, tuple((Path(f"{lock_path}.filelock") / "claims").iterdir()), lock.is_locked) == (
        (),
        (),
        False,
    )


def test_strict_soft_sentinel_inspection_failure_closes_descriptor(tmp_path: Path, mocker: MockerFixture) -> None:
    lock_path = tmp_path / "resource.lock"
    with StrictSoftFileLock(lock_path):
        pass
    real_fstat = os.fstat
    inspected_fd: int | None = None

    def fail_sentinel_inspection(fd: int) -> os.stat_result:
        nonlocal inspected_fd
        if os.lseek(fd, 0, os.SEEK_CUR) == len(_SENTINEL):
            inspected_fd = fd
            msg = "sentinel inspection failed"
            raise OSError(msg)
        return real_fstat(fd)

    mocker.patch("filelock._strict.os.fstat", side_effect=fail_sentinel_inspection)
    lock = StrictSoftFileLock(lock_path)

    with pytest.raises(OSError, match="sentinel inspection failed"):
        lock.acquire()
    assert inspected_fd is not None
    # A closed descriptor reports EBADF on every platform, but the message differs (Windows says "handle is invalid").
    with pytest.raises(OSError, match=r"Bad file descriptor|handle is invalid") as closed:
        real_fstat(inspected_fd)
    assert closed.value.errno == EBADF
    assert (lock.is_locked, lock.lock_counter, lock.claims) == (False, 0, ())


def test_strict_soft_publication_and_close_failures_preserve_both_errors(tmp_path: Path, mocker: MockerFixture) -> None:
    lock_path = tmp_path / "resource.lock"
    real_close = os.close
    close_failed = False

    def fail_first_close(fd: int) -> None:
        nonlocal close_failed
        real_close(fd)
        if not close_failed:
            close_failed = True
            msg = "private close failed"
            raise OSError(msg)

    mocker.patch("filelock._util.os.write", side_effect=OSError("record write failed"))
    mocker.patch("filelock._strict.os.close", side_effect=fail_first_close)
    lock = StrictSoftFileLock(lock_path)

    with pytest.raises(ExceptionGroup) as raised:
        lock.acquire()
    assert [str(error) for error in raised.value.exceptions] == ["record write failed", "private close failed"]
    assert (lock.is_locked, lock.lock_counter, tuple(tmp_path.glob("**/*.tmp"))) == (False, 0, ())


@pytest.mark.asyncio
async def test_async_strict_soft_matches_claim_protocol(tmp_path: Path) -> None:
    lock_path = tmp_path / "resource.lock"
    holder = AsyncStrictSoftFileLock(lock_path)

    async with holder:
        assert holder.claims[0].state == "held"
        with pytest.raises(Timeout):
            await AsyncStrictSoftFileLock(lock_path, timeout=0).acquire()
    async with AsyncStrictSoftFileLock(lock_path, timeout=0) as contender:
        assert contender.is_locked


@pytest.mark.skipif(sys.platform != "win32", reason="exercises Windows pathname sharing")
def test_strict_soft_windows_release_allows_reacquire(tmp_path: Path) -> None:
    lock_path = tmp_path / "resource.lock"

    with StrictSoftFileLock(lock_path) as first:
        assert first.claims[0].state == "held"
    with StrictSoftFileLock(lock_path, timeout=0) as second:
        assert second.claims[0].state == "held"
