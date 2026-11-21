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
    "itertools",
    "secrets",
    f"{__spec__.parent}._claims",
    f"{__spec__.parent}._files",
    f"{__spec__.parent}._scan",
]

import contextlib
import functools
import itertools
import os
import secrets
import stat
import sys

from filelock.internal.core import SoftFileLockProtocolError
from filelock.internal.filesystem import foreign_owner

from ._claims import (
    CLAIM_DIRECTORY_NAME,
    TOKEN_HEX_LENGTH,
    ClaimEntryLimitError,
    RetryDoorwayError,
    claim_name,
    is_claim_probe_name,
    is_reclaimable_private_name,
    parse_claim_name,
    private_public_name,
)
from ._files import SUPPORTS, lstat_relative, open_directory, pin_directories, scandir_relative
from ._scan import LIMITS, filesystem_now, reclaim_private_record

if TYPE_CHECKING:
    from pathlib import Path


def ensure_claim_directory(lock_file: str, directory: Path, mode: int) -> None:
    if not (SUPPORTS.open_dir_fd and SUPPORTS.mkdir_dir_fd):  # pragma: lacks dir-fd
        try:
            pinned = pin_directories(directory.parent, acquiring=True)
        except OSError as error:
            raise SoftFileLockProtocolError(lock_file, None, f"cannot open {directory.parent}") from error
        with pinned:
            ensure_protocol_directory(lock_file, directory, mode)
    else:  # pragma: needs dir-fd
        # mkdir follows a symlink in every component but the last, so create against a descriptor of the real parent.
        try:
            coordination_fd = open_directory(os.fspath(directory.parent))
        except OSError as error:
            raise SoftFileLockProtocolError(lock_file, None, f"cannot open {directory.parent}") from error
        try:
            if sys.platform != "win32":  # pragma: win32 no cover
                _refuse_unrelated_coordination_entries(lock_file, directory.parent, coordination_fd)
            ensure_protocol_directory(lock_file, directory, mode, coordination_fd)
        finally:
            with contextlib.suppress(OSError):
                os.close(coordination_fd)


def ensure_protocol_directory(lock_file: str, directory: Path, mode: int, parent_fd: int | None = None) -> None:
    try:
        if parent_fd is None:
            directory.mkdir(mode)
        else:  # pragma: needs dir-fd
            os.mkdir(directory.name, mode, dir_fd=parent_fd)
    except FileExistsError:
        try:
            found = lstat_relative((os.fspath(directory.parent), parent_fd), directory.name)
        except OSError as error:
            raise SoftFileLockProtocolError(lock_file, None, f"cannot inspect {directory}") from error
        if (
            not stat.S_ISDIR(found.st_mode)
            or getattr(found, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
        ):
            raise SoftFileLockProtocolError(lock_file, None, f"{directory} is not a real directory") from None
    # An inherited ACL can widen a directory. The sticky bit protects claims where other accounts can write.
    if sys.platform != "win32":  # pragma: win32 no cover
        try:
            fd = os.open(
                directory.name if parent_fd is not None else os.fspath(directory),
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=parent_fd,
            )
        except OSError as error:
            raise SoftFileLockProtocolError(lock_file, None, f"cannot open {directory}") from error
        try:
            found = _owned_protocol_directory(lock_file, directory, fd, parent_fd)
            if directory.name != CLAIM_DIRECTORY_NAME:
                _refuse_unrelated_coordination_entries(lock_file, directory, fd)
            _settle_opened_directory_mode(lock_file, directory, mode, fd, found)
        finally:
            os.close(fd)


def _owned_protocol_directory(
    lock_file: str, directory: Path, fd: int, parent_fd: int | None
) -> os.stat_result:  # pragma: win32 no cover
    try:
        found = os.fstat(fd)
        parent = directory.parent.stat() if parent_fd is None else os.fstat(parent_fd)
    except OSError as error:
        raise SoftFileLockProtocolError(lock_file, None, f"cannot set the mode of {directory}") from error
    if (owner := foreign_owner(found, parent, fd=fd)) is not None:  # pragma: needs foreign-sticky-tmp
        reason = f"{directory} belongs to uid {owner}, not to this process's account or the parent's owner"
        raise SoftFileLockProtocolError(lock_file, None, reason)
    return found


def _settle_opened_directory_mode(
    lock_file: str, directory: Path, mode: int, fd: int, found: os.stat_result
) -> None:  # pragma: win32 no cover
    settled = found.st_mode & 0o7000 | found.st_mode & mode & 0o777 | stat.S_ISVTX
    if directory.name == CLAIM_DIRECTORY_NAME:
        _refuse_unrelated_claim_entries(lock_file, directory, fd)
    if sys.platform != "win32" and found.st_uid == os.geteuid() and settled != stat.S_IMODE(found.st_mode):
        try:
            os.fchmod(fd, settled)
        except OSError as error:
            raise SoftFileLockProtocolError(lock_file, None, f"cannot set the mode of {directory}") from error


def _refuse_unrelated_claim_entries(lock_file: str, directory: Path, fd: int) -> None:  # pragma: win32 no cover
    try:
        with scandir_relative((os.fspath(directory), fd)) as entries:
            names = [entry.name for entry in itertools.islice(entries, LIMITS.entries + 1)]
        foreign = next(
            (
                name
                for name in names[: LIMITS.entries]
                if parse_claim_name(name) is None
                and not is_claim_probe_name(name)
                and parse_claim_name(private_public_name(name) or "") is None
                and not (
                    name.startswith(".nfs")
                    and len(name) > len(".nfs")
                    and all(c in "0123456789abcdef" for c in name[4:])
                )
            ),
            None,
        )
    except OSError as error:
        raise SoftFileLockProtocolError(lock_file, None, f"cannot list {directory}") from error
    if foreign is not None:
        raise SoftFileLockProtocolError(lock_file, foreign, "unknown claim name or protocol version")
    private = [name for name in names[: LIMITS.entries] if name.startswith(".")]
    if len(names) > LIMITS.entries:
        raise SoftFileLockProtocolError(
            lock_file, None, str(ClaimEntryLimitError(os.fspath(directory), LIMITS.entries, len(private)))
        )
    if len(names) - len(private) > LIMITS.claims:
        directory_ref = os.fspath(directory), fd
        now = functools.cache(
            functools.partial(
                filesystem_now, directory_ref, claim_name("held", secrets.token_hex(TOKEN_HEX_LENGTH // 2))
            )
        )
        try:
            for name in private[: LIMITS.claims]:
                if is_reclaimable_private_name(name):
                    reclaim_private_record(directory_ref, name, now)
        except OSError as error:
            reason = f"cannot reclaim private claim records: {error.strerror or type(error).__name__}"
            raise SoftFileLockProtocolError(lock_file, None, reason) from error
        raise RetryDoorwayError


def _refuse_unrelated_coordination_entries(lock_file: str, directory: Path, fd: int) -> None:  # pragma: win32 no cover
    try:
        with scandir_relative((os.fspath(directory), fd)) as entries:
            claim_entry = next(entries, None)
            unrelated = claim_entry is not None and (
                claim_entry.name != CLAIM_DIRECTORY_NAME
                or not claim_entry.is_dir(follow_symlinks=False)
                or next(entries, None) is not None
            )
    except OSError as error:
        raise SoftFileLockProtocolError(lock_file, None, f"cannot list {directory}") from error
    if unrelated:
        raise SoftFileLockProtocolError(lock_file, None, f"{directory} holds entries that are not this lock's")
    if claim_entry is None:
        return
    try:
        claims_fd = open_directory(
            claim_entry.name if SUPPORTS.open_dir_fd else claim_entry.path,
            dir_fd=fd if SUPPORTS.open_dir_fd else None,
        )
    except OSError as error:
        raise SoftFileLockProtocolError(
            lock_file, claim_entry.name, f"cannot open {directory / claim_entry.name}"
        ) from error
    try:
        _refuse_unrelated_claim_entries(lock_file, directory / claim_entry.name, claims_fd)
    finally:
        os.close(claims_fd)


__all__ = [
    "ensure_claim_directory",
    "ensure_protocol_directory",
]
