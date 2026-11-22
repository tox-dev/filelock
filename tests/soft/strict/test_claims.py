from __future__ import annotations

import os
import pickle  # ruff:ignore[suspicious-pickle-import]  # only local bytes are loaded
import time
from errno import EACCES, EIO, ENOENT, EPERM, ESTALE
from pathlib import Path
from typing import TYPE_CHECKING, Final, NoReturn

import pytest

from filelock import SoftFileLockProtocolError, StrictSoftFileLock
from filelock._soft._strict._directories import LIMITS
from filelock._soft._strict._scan import read_existing_claims

if TYPE_CHECKING:
    from collections.abc import Callable

    from pytest_mock import MockerFixture
from .claim_records import initialize_protocol, write_held_claim
from .filesystem_faults import PathValue, open_raising_for, route_record_opens_through_os_open

pytestmark: Final[pytest.MarkDecorator] = pytest.mark.requires_hard_links


def test_strict_soft_claim_directory_read_error_fails_closed(tmp_path: Path, mocker: MockerFixture) -> None:
    lock_path = tmp_path / "resource.lock"
    initialize_protocol(lock_path)
    mocker.patch(
        "filelock._soft._strict._scan.scandir_relative",
        side_effect=PermissionError(EACCES, "cannot list"),
        autospec=True,
    )

    with pytest.raises(SoftFileLockProtocolError, match="cannot list"):
        StrictSoftFileLock(lock_path).acquire()


@pytest.mark.parametrize(
    "content",
    [
        pytest.param(b"filelock-strict-v1\n" + b"0" * 32 + b"\n0\n00\n4242\n", id="zero-pid"),
        pytest.param(b"filelock-strict-v1\n" + b"1" * 32 + b"\n1\n00\n4242\n", id="wrong-token"),
        pytest.param(b"filelock-strict-v1\n" + b"0" * 32 + b"\n1\n686f7374\nnot-int\n", id="non-integer-start"),
        pytest.param(b"filelock-strict-v1\n" + b"0" * 32 + b"\n1\n686f7374\n-5\n", id="negative-start"),
        pytest.param(b"filelock-strict-v1\n" + b"0" * 32 + b"\n1\n686f7374\n007\n", id="noncanonical-start"),
    ],
)
def test_strict_soft_structurally_invalid_record_fails_closed(tmp_path: Path, content: bytes) -> None:
    lock_path = tmp_path / "resource.lock"
    claims = Path(f"{lock_path}.filelock") / "claims"
    claims.mkdir(parents=True)
    (claims / f"held-v1-{'0' * 32}.claim").write_bytes(content)

    with pytest.raises(SoftFileLockProtocolError, match="malformed claim record"):
        StrictSoftFileLock(lock_path).acquire()


def test_strict_soft_accepts_maximum_pid(tmp_path: Path) -> None:
    lock_path = tmp_path / "resource.lock"
    token = "0" * 32
    claims = Path(f"{lock_path}.filelock") / "claims"
    claims.mkdir(parents=True)
    (claims / f"held-v1-{token}.claim").write_text(
        f"filelock-strict-v1\n{token}\n2147483647\n686f7374\n4242\n",
        encoding="ascii",
        newline="",
    )

    assert StrictSoftFileLock(lock_path).claims[0].pid == 2**31 - 1


@pytest.mark.parametrize(
    ("pid", "hostname_hex"),
    [
        pytest.param("01", "686f7374", id="leading-zero-pid"),
        pytest.param("2147483648", "686f7374", id="pid-above-signed-int"),
        pytest.param("1", "686F7374", id="uppercase-hostname-hex"),
        pytest.param("1", "20686f737420", id="spaced-hostname"),
        pytest.param("1", "686f737400", id="null-hostname"),
        pytest.param("1", "1b686f7374", id="escape-hostname"),
    ],
)
def test_strict_soft_rejects_noncanonical_owner_record(tmp_path: Path, pid: str, hostname_hex: str) -> None:
    lock_path = tmp_path / "resource.lock"
    token = "0" * 32
    claims = Path(f"{lock_path}.filelock") / "claims"
    claims.mkdir(parents=True)
    (claims / f"held-v1-{token}.claim").write_text(
        f"filelock-strict-v1\n{token}\n{pid}\n{hostname_hex}\n4242\n",
        encoding="ascii",
        newline="",
    )

    with pytest.raises(SoftFileLockProtocolError, match="malformed claim record"):
        _ = StrictSoftFileLock(lock_path).claims


@pytest.mark.parametrize(
    "make_claim",
    [
        pytest.param(Path.mkdir, id="non-regular"),
        pytest.param(lambda path: path.write_bytes(b"x" * 1025), id="oversized"),
    ],
)
def test_strict_soft_invalid_record_node_fails_closed(tmp_path: Path, make_claim: Callable[[Path], object]) -> None:
    lock_path = tmp_path / "resource.lock"
    claims = Path(f"{lock_path}.filelock") / "claims"
    claims.mkdir(parents=True)
    make_claim(claims / f"held-v1-{'0' * 32}.claim")

    with pytest.raises(SoftFileLockProtocolError, match="cannot read claim"):
        StrictSoftFileLock(lock_path).acquire()


@pytest.mark.parametrize(
    ("replacement", "message"),
    [
        pytest.param("file", "changed while opening", id="different-file"),
        pytest.param("directory", "is not a regular file", id="directory"),
    ],
)
def test_strict_soft_claim_replaced_while_opening_fails_closed(
    tmp_path: Path, mocker: MockerFixture, replacement: str, message: str
) -> None:
    lock_path = tmp_path / "resource.lock"
    claims = Path(f"{lock_path}.filelock") / "claims"
    claims.mkdir(parents=True)
    claim = claims / f"held-v1-{'0' * 32}.claim"
    record = b"filelock-strict-v1\n" + b"0" * 32 + b"\n1\n686f7374\n"
    claim.write_bytes(record)
    real_open = os.open
    replaced = False

    def replace_before_open(
        path: PathValue,
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal replaced
        if not replaced and Path(os.fsdecode(path)).name == claim.name:
            replaced = True
            claim.replace(claim.with_name(f".{claim.name}.replaced"))
            if replacement == "file":
                claim.write_bytes(record)
            else:
                claim.mkdir()
        return real_open(path, flags, mode, dir_fd=dir_fd)

    route_record_opens_through_os_open(mocker)
    mocker.patch("os.open", side_effect=replace_before_open, autospec=True)

    with pytest.raises(SoftFileLockProtocolError, match=message):
        StrictSoftFileLock(lock_path).acquire()


def test_strict_soft_stale_claim_read_is_skipped(tmp_path: Path, mocker: MockerFixture) -> None:
    # After a peer unlinks an NFS claim, its cached handle can keep returning ESTALE.
    lock_path = tmp_path / "resource.lock"
    claim = write_held_claim(Path(f"{lock_path}.filelock") / "claims")
    mocker.patch.object(type(LIMITS), "read_grace", 0.02)
    open_raising_for(claim, OSError(ESTALE, "Stale file handle"), mocker)

    assert StrictSoftFileLock(lock_path).claims == ()


def test_strict_soft_stale_cached_claim_is_skipped(tmp_path: Path, mocker: MockerFixture) -> None:
    lock_path = tmp_path / "resource.lock"
    claim = write_held_claim(Path(f"{lock_path}.filelock") / "claims")
    mocker.patch("os.link", autospec=True, side_effect=OSError(ESTALE, "Stale file handle"))

    assert read_existing_claims(str(lock_path), claim.parent, entry_limit=LIMITS.entries, verify_server=True) == ()


def test_strict_soft_live_claim_survives_server_probe(tmp_path: Path) -> None:
    lock_path = tmp_path / "resource.lock"
    claim = write_held_claim(Path(f"{lock_path}.filelock") / "claims")

    found = read_existing_claims(str(lock_path), claim.parent, entry_limit=LIMITS.entries, verify_server=True)

    assert [item.name for item in found] == [claim.name]
    assert [item.name for item in claim.parent.iterdir()] == [claim.name]


def test_strict_soft_claim_it_may_not_link_counts_as_published(tmp_path: Path, mocker: MockerFixture) -> None:
    # Linux's fs.protected_hardlinks refuses a link to another account's claim, which only that account may write.
    lock_path = tmp_path / "resource.lock"
    claim = write_held_claim(Path(f"{lock_path}.filelock") / "claims")
    mocker.patch("os.link", autospec=True, side_effect=PermissionError(EPERM, "not permitted"))

    found = read_existing_claims(str(lock_path), claim.parent, entry_limit=LIMITS.entries, verify_server=True)

    assert [item.name for item in found] == [claim.name]
    assert [item.name for item in claim.parent.iterdir()] == [claim.name]


def test_strict_soft_claim_probe_error_fails_closed(tmp_path: Path, mocker: MockerFixture) -> None:
    lock_path = tmp_path / "resource.lock"
    claim = write_held_claim(Path(f"{lock_path}.filelock") / "claims")
    mocker.patch("os.link", autospec=True, side_effect=OSError(EIO, "I/O error"))

    with pytest.raises(SoftFileLockProtocolError, match="cannot verify claim"):
        read_existing_claims(str(lock_path), claim.parent, entry_limit=LIMITS.entries, verify_server=True)


def test_strict_soft_claim_probe_cleanup_error_fails_closed(tmp_path: Path, mocker: MockerFixture) -> None:
    lock_path = tmp_path / "resource.lock"
    claim = write_held_claim(Path(f"{lock_path}.filelock") / "claims")
    mocker.patch("os.unlink", autospec=True, side_effect=OSError(EIO, "I/O error"))

    with pytest.raises(SoftFileLockProtocolError, match="cannot remove claim probe"):
        read_existing_claims(str(lock_path), claim.parent, entry_limit=LIMITS.entries, verify_server=True)


def test_strict_soft_claim_probe_removed_by_peer(tmp_path: Path, mocker: MockerFixture) -> None:
    lock_path = tmp_path / "resource.lock"
    claim = write_held_claim(Path(f"{lock_path}.filelock") / "claims")
    real_unlink = os.unlink

    def remove_before_cleanup(path: PathValue, *, dir_fd: int | None = None) -> NoReturn:
        real_unlink(path, dir_fd=dir_fd)
        raise FileNotFoundError(ENOENT, "already removed")

    mocker.patch("os.unlink", autospec=True, side_effect=remove_before_cleanup)

    assert [
        item.name
        for item in read_existing_claims(str(lock_path), claim.parent, entry_limit=LIMITS.entries, verify_server=True)
    ] == [claim.name]


def test_strict_soft_claim_probe_requires_hard_links(tmp_path: Path, mocker: MockerFixture) -> None:
    lock_path = tmp_path / "resource.lock"
    claim = write_held_claim(Path(f"{lock_path}.filelock") / "claims")
    mocker.patch("os.link", autospec=True, side_effect=NotImplementedError)

    with pytest.raises(SoftFileLockProtocolError, match="hard links unavailable"):
        read_existing_claims(str(lock_path), claim.parent, entry_limit=LIMITS.entries, verify_server=True)


def test_strict_soft_abandoned_claim_probe_is_reclaimed(tmp_path: Path) -> None:
    lock_path = tmp_path / "resource.lock"
    claim_directory = Path(f"{lock_path}.filelock") / "claims"
    claim_directory.mkdir(parents=True)
    probe = claim_directory / f".probe-v1-{'0' * 32}.tmp"
    probe.write_bytes(b"abandoned")
    os.utime(probe, (time.time() - 10, time.time() - 10))

    with StrictSoftFileLock(lock_path, timeout=0):
        assert probe.exists() is (os.name == "nt")


def test_strict_soft_stale_claim_read_revalidates_to_gone(tmp_path: Path, mocker: MockerFixture) -> None:
    lock_path = tmp_path / "resource.lock"
    claim = write_held_claim(Path(f"{lock_path}.filelock") / "claims")
    real_open = os.open
    stale_raised = False

    def stale_then_vanish(path: PathValue, flags: int, mode: int = 0o777, *, dir_fd: int | None = None) -> int:
        nonlocal stale_raised
        if not stale_raised and Path(os.fsdecode(path)).name == claim.name:
            stale_raised = True
            claim.unlink()
            raise OSError(ESTALE, "Stale file handle")
        # The retry re-lstats the vanished claim and fails there before reopening, so only the directory walk passes.
        return real_open(path, flags, mode, dir_fd=dir_fd)  # pragma: needs dir-fd

    route_record_opens_through_os_open(mocker)
    mocker.patch("os.open", side_effect=stale_then_vanish, autospec=True)

    assert StrictSoftFileLock(lock_path).claims == ()
    assert stale_raised


def test_strict_soft_io_error_claim_read_fails_closed(tmp_path: Path, mocker: MockerFixture) -> None:
    lock_path = tmp_path / "resource.lock"
    open_raising_for(write_held_claim(Path(f"{lock_path}.filelock") / "claims"), OSError(EIO, "I/O error"), mocker)

    with pytest.raises(SoftFileLockProtocolError, match="cannot read claim"):
        _ = StrictSoftFileLock(lock_path).claims


def test_strict_soft_protocol_error_pickles() -> None:
    error = SoftFileLockProtocolError("resource.lock", "held-v2-claim", "unknown version")

    restored = pickle.loads(pickle.dumps(error))  # ruff:ignore[suspicious-pickle-usage]  # input comes from the preceding local value

    assert (type(restored), restored.lock_file, restored.claim_name, restored.reason, str(restored)) == (
        SoftFileLockProtocolError,
        "resource.lock",
        "held-v2-claim",
        "unknown version",
        "Invalid strict soft-lock state at resource.lock: claim 'held-v2-claim': unknown version",
    )
