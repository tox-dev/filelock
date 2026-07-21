from __future__ import annotations

import errno
import os
import socket
import sys
import time
from errno import EBADF, ENOSYS, EXDEV
from pathlib import Path
from typing import TYPE_CHECKING, Final

import pytest
from capability_marks import NEEDS_FILE_MODE

if sys.version_info >= (3, 11):
    from builtins import ExceptionGroup  # pragma: >=3.11 cover
else:  # pragma: <3.11 cover
    from exceptiongroup import ExceptionGroup

from coverage_pragmas import CAPABILITIES

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
from filelock._strict import _PRIVATE_RECORD_MARKER, _probe_hard_link_unsupported_errnos

if TYPE_CHECKING:
    from pytest_mock import MockerFixture

_SENTINEL: Final[bytes] = b"1\nfilelock-strict-v1\x00\n0\n"


pytestmark = pytest.mark.requires_hard_links


def test_strict_soft_acquire_publishes_owner_claim(tmp_path: Path) -> None:
    lock_path = tmp_path / "resource.lock"
    lock = StrictSoftFileLock(lock_path)

    with lock:
        held, intent = lock.claims
        owner = {
            "token": held.token,
            "pid": os.getpid(),
            "hostname": socket.gethostname(),
            "start": process_start_token(os.getpid()),
        }
        assert (held, intent) == (
            StrictSoftFileClaim(name=held.name, state="held", **owner),
            StrictSoftFileClaim(name=intent.name, state="intent", **owner),
        )
    assert (lock_path.read_bytes(), lock.claims, lock.is_locked) == (_SENTINEL, (), False)


def test_strict_soft_is_reentrant(tmp_path: Path) -> None:
    lock = StrictSoftFileLock(tmp_path / "resource.lock")

    with lock:
        with lock:
            assert (lock.is_locked, lock.lock_counter, len(lock.claims)) == (True, 2, 2)
        assert (lock.is_locked, lock.lock_counter, len(lock.claims)) == (True, 1, 2)
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

    breaker = StrictSoftFileLock(lock_path)
    for claim in holder.claims:
        breaker.force_break(claim.name)
    with StrictSoftFileLock(lock_path, timeout=0) as contender:
        assert (holder.is_locked, contender.is_locked) == (True, True)
    holder.release()


def test_strict_soft_force_break_requires_exact_case(tmp_path: Path) -> None:
    holder = StrictSoftFileLock(tmp_path / "resource.lock")
    holder.acquire()
    names = [claim.name for claim in holder.claims]

    with pytest.raises(FileNotFoundError):
        StrictSoftFileLock(holder.lock_file).force_break(names[0].upper())
    assert [claim.name for claim in holder.claims] == names
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


@NEEDS_FILE_MODE  # pragma: needs file-mode
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


@pytest.mark.skipif(not CAPABILITIES["symlink"], reason="stages a claim as a symlink")
def test_strict_soft_symlink_claim_fails_closed(tmp_path: Path) -> None:  # pragma: needs symlink
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


@pytest.mark.parametrize(
    ("defined", "expected"),
    [
        pytest.param({"ENOTSUP": 4242, "EOPNOTSUPP": 4343}, {ENOSYS, EXDEV, 4242}, id="enotsup"),
        pytest.param({"EOPNOTSUPP": 4343}, {ENOSYS, EXDEV, 4343}, id="eopnotsupp"),
        pytest.param({}, {ENOSYS, EXDEV}, id="neither"),
    ],
)
def test_strict_soft_hard_link_unsupported_errnos(
    monkeypatch: pytest.MonkeyPatch, defined: dict[str, int], expected: set[int]
) -> None:
    for name in ("ENOTSUP", "EOPNOTSUPP"):
        monkeypatch.delattr(errno, name, raising=False)
    for name, code in defined.items():
        monkeypatch.setattr(errno, name, code, raising=False)

    assert _probe_hard_link_unsupported_errnos() == expected


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
        # Only a dir_fd release closes a second descriptor, so elsewhere the first close is the only one.
        if not close_failed:  # pragma: no branch
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


# The reacquire only needs proving where the holder's own open handle blocks the unlink; elsewhere release
# removes the name outright and there is no sharing rule to exercise.
@pytest.mark.skipif(
    CAPABILITIES["unlink-open-file"], reason="exercises pathname sharing where an open file resists unlink"
)
def test_strict_soft_release_allows_reacquire(tmp_path: Path) -> None:  # pragma: lacks unlink-open-file
    lock_path = tmp_path / "resource.lock"

    with StrictSoftFileLock(lock_path) as first:
        assert first.claims[0].state == "held"
    with StrictSoftFileLock(lock_path, timeout=0) as second:
        assert second.claims[0].state == "held"


def test_strict_soft_reclaims_an_aged_sentinel_private_record(tmp_path: Path) -> None:
    # A crash between creating a private record and linking it leaves it behind; the next acquire reclaims it once
    # it ages out. The dir_fd reaper cases only proved this where dir_fd exists.
    lock_path = tmp_path / "resource.lock"
    stale = tmp_path / f".{lock_path.name}{_PRIVATE_RECORD_MARKER}{'0' * 32}.tmp"
    stale.write_bytes(b"")
    aged = time.time() - 3600
    os.utime(stale, (aged, aged))

    with StrictSoftFileLock(lock_path):
        assert not stale.exists()
