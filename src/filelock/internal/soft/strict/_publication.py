from __future__ import annotations

from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from importlib.machinery import ModuleSpec

__spec__ = cast("ModuleSpec", __spec__)

__lazy_modules__ = [
    "contextlib",
    "filelock.internal.core",
    "filelock.internal.filesystem",
    "pathlib",
    f"{__spec__.parent}._claims",
    f"{__spec__.parent}._files",
]

import contextlib
import os
import stat
import sys
from pathlib import Path

from filelock.internal.core import raise_cleanup_errors
from filelock.internal.filesystem import write_all

from ._files import (
    O_BINARY,
    SUPPORTS,
    file_identity,
    link_or_find_committed,
    open_claim_directory,
    open_directory,
    open_relative,
    pin_claim_directory,
    relative_identity,
    unlink_in_directory,
    unlink_relative,
)

if sys.platform == "win32":  # pragma: win32 cover
    from filelock.internal.filesystem.windows import (
        cancelling_oplock_wait,
    )

if TYPE_CHECKING:
    from collections.abc import Callable

    from filelock.internal.core import FileLockContext


from ._claims import PrivateRecordReclaimedError, private_record_name


def raise_recorded_errors(message: str, errors: list[BaseException]) -> None:
    if len(errors) > 1:
        raise_cleanup_errors(message, errors[0], *errors[1:])
    raise errors[0]


def publish_record(
    public_path: str,
    record: bytes,
    mode: int,
    directory_opener: Callable[[str], int] | None = None,
) -> BaseException | None:
    directory, public_name = os.path.split(public_path)
    directory = directory or os.curdir
    private_name = private_record_name(public_name)
    directory_fd = (directory_opener or open_directory)(directory) if SUPPORTS.open_dir_fd else None
    directory_ref = directory, directory_fd
    try:
        _publish_record_in_directory(directory_ref, (private_name, public_name), mode, record)
    except BaseException as publication_error:  # preserve publication and directory cleanup errors
        try:
            if directory_fd is not None:  # pragma: needs dir-fd
                os.close(directory_fd)
        except BaseException as close_error:  # ruff:ignore[blind-except]  # pragma: needs dir-fd  # preserve publication and directory cleanup errors
            raise_cleanup_errors("strict publication directory cleanup failed", publication_error, close_error)
        raise
    if directory_fd is not None:  # pragma: needs dir-fd
        try:
            os.close(directory_fd)
        except BaseException as close_error:  # ruff:ignore[blind-except]  # a claim's caller recorded its path before publishing
            return close_error
    return None


def _publish_record_in_directory(
    directory_ref: tuple[str, int | None],
    names: tuple[str, str],
    mode: int,
    record: bytes,
) -> None:
    flags = os.O_RDWR | os.O_CREAT | os.O_EXCL | O_BINARY
    if (o_nofollow := getattr(os, "O_NOFOLLOW", None)) is not None:  # pragma: needs o-nofollow
        flags |= o_nofollow
    private_fd = open_relative(directory_ref, names[0], flags, mode & ~(stat.S_IWGRP | stat.S_IWOTH))
    private_identity: tuple[int, int] | None = None
    try:
        private_identity = file_identity(os.fstat(private_fd))
        write_all(private_fd, record)
        _link_private_record(directory_ref, names, private_identity)
    except BaseException as publication_error:  # preserve publication and cleanup errors
        close_error, unlink_error = _close_and_unlink_private_record(
            directory_ref,
            names[0],
            private_fd,
            private_identity,
        )
        if close_error is not None or unlink_error is not None:
            raise_cleanup_errors(
                "strict record publication cleanup failed",
                publication_error,
                close_error,
                unlink_error,
            )
        raise
    close_error, unlink_error = _close_and_unlink_private_record(
        directory_ref,
        names[0],
        private_fd,
        private_identity,
    )
    if close_error is not None or unlink_error is not None:
        _raise_record_finalization_errors(close_error, unlink_error)


def _link_private_record(
    directory_ref: tuple[str, int | None],
    names: tuple[str, str],
    private_identity: tuple[int, int],
) -> None:
    try:
        link_or_find_committed(directory_ref, *names, private_identity)
    except FileNotFoundError as error:
        if relative_identity(directory_ref, names[0]) is not None:
            raise
        msg = "private publication record was reclaimed"
        raise PrivateRecordReclaimedError(msg) from error
    if relative_identity(directory_ref, names[1]) == private_identity:
        return
    msg_0 = "private publication record was replaced"
    raise PrivateRecordReclaimedError(msg_0)


def _close_and_unlink_private_record(
    directory_ref: tuple[str, int | None],
    private_name: str,
    private_fd: int,
    private_identity: tuple[int, int] | None,
) -> tuple[BaseException | None, BaseException | None]:
    close_error: BaseException | None = None
    try:
        os.close(private_fd)
    except BaseException as error:  # ruff:ignore[blind-except]  # returned for grouping with unlink failures
        close_error = error
    unlink_error: BaseException | None = None
    try:
        if private_identity is None:
            unlink_relative(directory_ref, private_name)
        elif relative_identity(directory_ref, private_name) == private_identity:
            with contextlib.suppress(FileNotFoundError):
                unlink_relative(directory_ref, private_name)
    except FileNotFoundError:
        pass
    except BaseException as error:  # ruff:ignore[blind-except]  # returned for grouping with close failures
        unlink_error = error
    return close_error, unlink_error


def _raise_record_finalization_errors(
    close_error: BaseException | None,
    unlink_error: BaseException | None,
) -> None:
    errors = [error for error in (close_error, unlink_error) if error is not None]
    if len(errors) > 1:
        raise_cleanup_errors("strict record finalization failed", errors[0], *errors[1:])
    raise errors[0]


def unlink_owner_path_result(
    path: str, context: FileLockContext, *, deadline: float | None
) -> tuple[bool, BaseException | None]:
    directory, name = os.path.split(path)
    try:
        if sys.platform == "win32":  # pragma: win32 cover
            if context.claim_descriptors is None or (fd := context.claim_descriptors.get(path)) is None:
                return True, None
            return True, unlink_in_directory(
                Path(directory),
                name,
                claim_fd=fd,
                cancel_wait=cancelling_oplock_wait(deadline) if deadline is not None else None,
            )
        with pin_claim_directory(claim_directory := Path(directory or os.curdir)):  # pragma: win32 no cover
            return True, unlink_in_directory(claim_directory, name)
    except FileNotFoundError:
        return True, None
    except BaseException as error:  # ruff:ignore[blind-except]  # keep ownership when unlink did not commit
        return False, error


def link_no_replace(  # pragma: win32 no cover
    directory: Path, source_name: str, destination_name: str
) -> BaseException | None:
    if SUPPORTS.link_dir_fd:  # pragma: needs dir-fd
        directory_fd = open_claim_directory(str(directory))
        try:
            link_or_find_committed((str(directory), directory_fd), source_name, destination_name)
        except BaseException as link_error:  # preserve link and directory cleanup errors
            try:
                os.close(directory_fd)
            except BaseException as close_error:  # ruff:ignore[blind-except]  # preserve link and directory cleanup errors
                raise_cleanup_errors("strict link directory cleanup failed", link_error, close_error)
            raise
        try:
            os.close(directory_fd)
        except BaseException as close_error:  # ruff:ignore[blind-except]  # the caller recorded the held path before linking
            return close_error
        return None
    with pin_claim_directory(directory):  # pragma: win32 cover
        link_or_find_committed((str(directory), None), source_name, destination_name)
    return None  # pragma: win32 cover


__all__ = [
    "link_no_replace",
    "publish_record",
    "raise_recorded_errors",
    "unlink_owner_path_result",
]
