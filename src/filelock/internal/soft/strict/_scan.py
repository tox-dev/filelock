from __future__ import annotations

from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from importlib.machinery import ModuleSpec

__spec__ = cast("ModuleSpec", __spec__)

__lazy_modules__ = [
    "contextlib",
    "filelock.internal.core",
    "filelock.internal.filesystem",
    "functools",
    "pathlib",
    "secrets",
    f"{__spec__.parent}._claims",
    f"{__spec__.parent}._files",
]

import contextlib
import functools
import os
import secrets
import stat
import sys
import time
from dataclasses import dataclass
from errno import EACCES, ENOENT, EPERM, ESTALE
from pathlib import Path
from typing import Final

from filelock.internal.core import SoftFileLockProtocolError
from filelock.internal.filesystem import write_all

from ._files import (
    O_BINARY,
    SUPPORTS,
    link_or_find_committed,
    open_claim_directory,
    open_record,
    open_relative,
    pin_claim_directory,
    scandir_relative,
    unlink_private_record_once,
    unlink_relative,
)

if sys.platform == "win32":  # pragma: win32 cover
    from filelock.internal.filesystem.windows import (
        is_unlinked,
    )

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator


from ._claims import (
    CLAIM_PROBE_PREFIX,
    CLAIM_RECORD_LIMIT,
    PRIVATE_RECORD_SUFFIX,
    TOKEN_HEX_LENGTH,
    ClaimEntryLimitError,
    RetryDoorwayError,
    StrictSoftFileClaim,
    StrictSoftFileClaimState,
    claim_name,
    is_reclaimable_private_name,
    parse_claim,
    parse_claim_name,
    private_record_name,
)


@dataclass(frozen=True, slots=True, kw_only=True)
class _ScanLimits:
    claims: int
    entries: int
    read_grace: float


# Scans include private entries, but only public claims count toward the contention limit.
LIMITS: Final[_ScanLimits] = _ScanLimits(claims=1024, entries=16_384, read_grace=0.5)
CLAIM_READ_RETRY: Final[float] = 0.002


def read_existing_claims(
    lock_file: str,
    directory: Path,
    *,
    entry_limit: int | None,
    verify_server: bool = sys.platform != "win32",
) -> tuple[StrictSoftFileClaim, ...]:
    if not SUPPORTS.open_dir_fd:  # pragma: lacks dir-fd
        try:
            pinned = pin_claim_directory(directory, acquiring=entry_limit is not None)
        except FileNotFoundError:
            return ()
        except OSError as error:
            reason = f"cannot open claim directory: {error.strerror or type(error).__name__}"
            raise SoftFileLockProtocolError(lock_file, None, reason) from error
        with pinned:
            return _read_claims(
                lock_file, (os.fspath(directory), None), entry_limit=entry_limit, verify_server=verify_server
            )
    try:  # pragma: needs dir-fd
        directory_fd = open_claim_directory(os.fspath(directory))
    except FileNotFoundError:  # pragma: needs dir-fd
        return ()
    except OSError as error:  # pragma: needs dir-fd
        reason = f"cannot open claim directory: {error.strerror or type(error).__name__}"
        raise SoftFileLockProtocolError(lock_file, None, reason) from error
    try:  # pragma: needs dir-fd
        return _read_claims(
            lock_file, (os.fspath(directory), directory_fd), entry_limit=entry_limit, verify_server=verify_server
        )
    finally:  # pragma: needs dir-fd
        with contextlib.suppress(OSError):
            os.close(directory_fd)


def _read_claims(
    lock_file: str, directory_ref: tuple[str, int | None], *, entry_limit: int | None, verify_server: bool
) -> tuple[StrictSoftFileClaim, ...]:
    names: Final[list[str]] = _list_claim_names(lock_file, directory_ref, entry_limit=entry_limit)
    name_parts: dict[str, tuple[StrictSoftFileClaimState, str]] = {}
    for name in names:
        if (parts := parse_claim_name(name)) is None:
            raise SoftFileLockProtocolError(lock_file, name, "unknown claim name or protocol version")
        name_parts[name] = parts
    records: Final[dict[str, bytes]] = _read_claim_records(lock_file, directory_ref, names, entry_limit=entry_limit)
    return tuple(
        parse_claim(lock_file, name, name_parts[name], records[name])
        for name in names
        if name in records and (not verify_server or _claim_exists_on_server(lock_file, directory_ref, name))
    )


def _list_claim_names(lock_file: str, directory_ref: tuple[str, int | None], *, entry_limit: int | None) -> list[str]:
    try:
        with scandir_relative(directory_ref) as entries:
            return _public_claim_names(lock_file, directory_ref, entries, entry_limit=entry_limit)
    except OSError as error:
        reason = f"cannot list claim directory: {error.strerror or type(error).__name__}"
        raise SoftFileLockProtocolError(lock_file, None, reason) from error


def _public_claim_names(
    lock_file: str,
    directory_ref: tuple[str, int | None],
    entries: Iterator[os.DirEntry[str]],
    *,
    entry_limit: int | None,
) -> list[str]:
    names: list[str] = []
    now: Callable[[], float | None] = (
        functools.cache(
            functools.partial(
                filesystem_now, directory_ref, claim_name("held", secrets.token_hex(TOKEN_HEX_LENGTH // 2))
            )
        )
        if entry_limit is not None
        else lambda: None
    )
    reclaims = hidden = 0
    for scanned, entry in enumerate(entries):
        if entry_limit is not None and scanned >= entry_limit:
            raise ClaimEntryLimitError(directory_ref[0], scanned, hidden)
        if not entry.name.startswith("."):
            names.append(entry.name)
            if entry_limit is not None and len(names) > LIMITS.claims:
                _refuse_excess_claims(lock_file, names)
            continue
        hidden += 1
        if reclaims < LIMITS.claims and is_reclaimable_private_name(entry.name):
            reclaims += 1
            reclaim_private_record(directory_ref, entry.name, now)
    return sorted(names)


def filesystem_now(directory_ref: tuple[str, int | None], public_name: str) -> float | None:  # pragma: win32 no cover
    # Use the filesystem clock to age records across hosts. A private probe can be reclaimed after a crash.
    probe = private_record_name(public_name)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | O_BINARY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = open_relative(directory_ref, probe, flags, 0o600)
    except OSError:
        return None
    try:
        write_all(fd, b"\n")
        return os.fstat(fd).st_mtime
    except OSError:
        return None
    finally:
        with contextlib.suppress(OSError):
            os.close(fd)
        with contextlib.suppress(OSError):
            unlink_private_record_once(directory_ref, probe)


def _refuse_excess_claims(lock_file: str, names: list[str]) -> None:
    for name in names:
        if parse_claim_name(name) is None:
            raise SoftFileLockProtocolError(lock_file, name, "unknown claim name or protocol version")
    raise RetryDoorwayError


def reclaim_private_record(
    directory_ref: tuple[str, int | None], private_name: str, now: Callable[[], float | None]
) -> None:
    directory, directory_fd = directory_ref
    try:
        private_stat = (
            os.stat(private_name, dir_fd=directory_fd, follow_symlinks=False)
            if directory_fd is not None and SUPPORTS.stat_dir_fd
            else Path(directory, private_name).lstat()
        )
    except FileNotFoundError:
        return
    if not stat.S_ISREG(private_stat.st_mode):
        msg = f"{Path(directory, private_name)} is not a regular private record"
        raise OSError(msg)
    # Closing a delete handle can wait indefinitely for another process's read-handle oplock.
    if sys.platform != "win32":  # pragma: win32 no cover
        private_record_grace: Final[float] = 2.0
        if private_stat.st_nlink == 1 and (
            (current := now()) is None or current - private_stat.st_mtime < private_record_grace
        ):
            return
        try:
            unlink_private_record_once(directory_ref, private_name)
        except FileNotFoundError:
            pass
        except OSError as error:
            if error.errno not in {EACCES, EPERM}:
                raise


def _read_claim_records(
    lock_file: str, directory_ref: tuple[str, int | None], names: list[str], *, entry_limit: int | None
) -> dict[str, bytes]:
    # Windows deletion and NFS revalidation can delay a read. One deadline bounds the whole scan.
    records: dict[str, bytes] = {}
    deadline: float | None = None
    while True:
        pending: dict[str, OSError] = {}
        for name in names:
            record, error = _attempt_claim_read(lock_file, directory_ref, name)
            if error is not None:
                pending[name] = error
            elif record is not None:
                records[name] = record
        if not pending:
            return records
        if deadline is None:
            deadline = time.monotonic() + LIMITS.read_grace
        elif time.monotonic() >= deadline:
            break
        names = list(pending)
        time.sleep(CLAIM_READ_RETRY)
    # After revalidation, ESTALE identifies a name the NFS server no longer has.
    if denied := [name for name, error in pending.items() if error.errno != ESTALE]:
        # Windows can deny opens after a peer removes the claim from the directory.
        listed: Final[set[str]] = set(_list_claim_names(lock_file, directory_ref, entry_limit=entry_limit))
        if still_listed := [name for name in denied if name in listed]:
            error = pending[still_listed[0]]
            reason = f"cannot read claim: {error.strerror or str(error) or type(error).__name__}"
            raise SoftFileLockProtocolError(lock_file, still_listed[0], reason) from error
    return records


def _attempt_claim_read(
    lock_file: str, directory_ref: tuple[str, int | None], name: str
) -> tuple[bytes | None, OSError | None]:
    try:
        fd, record = open_record(directory_ref, name, CLAIM_RECORD_LIMIT, deletable=True)
        os.close(fd)
    except FileNotFoundError:
        return None, None
    except PermissionError as error:
        if sys.platform == "win32" and is_unlinked(str(Path(directory_ref[0], name))):  # pragma: win32 cover
            return None, None
        return None, error
    except OSError as error:
        if error.errno == ESTALE:
            return None, error
        reason = f"cannot read claim: {error.strerror or str(error) or type(error).__name__}"
        raise SoftFileLockProtocolError(lock_file, name, reason) from error
    else:
        return record, None


def _claim_exists_on_server(lock_file: str, directory_ref: tuple[str, int | None], name: str) -> bool:
    # NFS may cache a vanished name. A hard link checks whether the server still has it.
    proof = f"{CLAIM_PROBE_PREFIX}{secrets.token_hex(TOKEN_HEX_LENGTH // 2)}{PRIVATE_RECORD_SUFFIX}"
    try:
        link_or_find_committed(directory_ref, name, proof)
    except OSError as error:
        if error.errno in {ENOENT, ESTALE}:
            return False
        if error.errno == EPERM:
            # Linux may refuse a hard link to another account's claim; it still counts as held.
            return True
        reason = f"cannot verify claim: {error.strerror or type(error).__name__}"
        raise SoftFileLockProtocolError(lock_file, name, reason) from error
    except NotImplementedError as error:
        raise SoftFileLockProtocolError(lock_file, name, "cannot verify claim: hard links unavailable") from error
    try:
        unlink_relative(directory_ref, proof)
    except FileNotFoundError:
        pass
    except OSError as error:
        reason = f"cannot remove claim probe: {error.strerror or type(error).__name__}"
        raise SoftFileLockProtocolError(lock_file, name, reason) from error
    return True


__all__ = [
    "CLAIM_READ_RETRY",
    "LIMITS",
    "filesystem_now",
    "read_existing_claims",
    "reclaim_private_record",
]
