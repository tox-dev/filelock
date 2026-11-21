from __future__ import annotations

from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from importlib.machinery import ModuleSpec

__spec__ = cast("ModuleSpec", __spec__)

__lazy_modules__ = [
    "contextlib",
    "filelock.internal.core",
    "pathlib",
    "tempfile",
    f"{__spec__.parent}._claims",
    f"{__spec__.parent}._windows",
]

import contextlib
import errno
import os
import stat
import sys
import tempfile
import time
from dataclasses import dataclass
from errno import EACCES, EINVAL, ENOSYS, EPERM, EXDEV
from pathlib import Path
from typing import Final

from filelock.internal.core import SoftFileLockProtocolError, raise_cleanup_errors

if sys.platform == "win32":  # pragma: win32 cover
    from filelock.internal.filesystem.windows import (
        DirectoryPinBlockedError,
        open_deletable_for_reading,
        open_read_without_oplock_wait,
        pin_directory,
        unpin_directory,
    )

    from ._windows import mark_claim_for_deletion

if TYPE_CHECKING:
    from collections.abc import Iterator


from ._claims import RetryDoorwayError, private_record_name

# Binary mode prevents Windows newline translation in protocol records.
O_BINARY: Final[int] = getattr(os, "O_BINARY", 0)


# A replaced os function no longer compares equal to the original in os.supports_dir_fd.
@dataclass(frozen=True, slots=True, kw_only=True)
class _FileSupport:
    open_dir_fd: bool
    unlink_dir_fd: bool
    stat_dir_fd: bool
    link_dir_fd: bool
    link_follow_symlinks: bool
    scandir_fd: bool
    mkdir_dir_fd: bool
    has_link: bool


_HAS_LINK: Final[bool] = hasattr(os, "link")
SUPPORTS: Final[_FileSupport] = _FileSupport(
    open_dir_fd=os.open in os.supports_dir_fd,
    unlink_dir_fd=os.unlink in os.supports_dir_fd,
    stat_dir_fd=os.stat in os.supports_dir_fd,
    link_dir_fd=_HAS_LINK and os.link in os.supports_dir_fd,
    link_follow_symlinks=_HAS_LINK and os.link in os.supports_follow_symlinks,
    scandir_fd=os.scandir in os.supports_fd,
    mkdir_dir_fd=os.mkdir in os.supports_dir_fd,
    has_link=_HAS_LINK,
)


def _probe_link_follow_symlinks() -> bool:
    # Some runtimes advertise no-follow links but reject them. A failed temporary directory cannot test support.
    if not SUPPORTS.has_link:
        return False
    try:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory, "probe-source")
            source.touch()
            return _probe_link_option(source, Path(directory, "probe-link"))
    except OSError:
        return SUPPORTS.link_follow_symlinks


def _probe_link_option(source: Path, destination: Path) -> bool:
    try:
        os.link(source, destination, follow_symlinks=False)
    except (NotImplementedError, ValueError):
        return False
    except OSError as error:
        return error.errno != EINVAL and SUPPORTS.link_follow_symlinks
    return True


_LINK_HONORS_FOLLOW_SYMLINKS: Final[bool] = _probe_link_follow_symlinks()


def _probe_hard_link_unsupported_errnos() -> frozenset[int]:
    # Some runtimes omit ENOTSUP. Prefer its native value where the two errnos differ.
    not_supported = getattr(errno, "ENOTSUP", getattr(errno, "EOPNOTSUPP", None))
    return frozenset({ENOSYS, EXDEV} if not_supported is None else {ENOSYS, EXDEV, not_supported})


_HARD_LINK_UNSUPPORTED_ERRNOS: Final[frozenset[int]] = _probe_hard_link_unsupported_errnos()


def unlink_private_record_once(  # pragma: win32 no cover
    directory_ref: tuple[str, int | None], private_name: str
) -> None:
    directory, directory_fd = directory_ref
    if directory_fd is not None and SUPPORTS.unlink_dir_fd:  # pragma: needs dir-fd
        os.unlink(private_name, dir_fd=directory_fd)
    else:
        Path(directory, private_name).unlink()


def open_record(
    directory_ref: tuple[str, int | None], name: str, limit: int, *, deletable: bool = False
) -> tuple[int, bytes]:
    path = Path(directory_ref[0], name)
    path_stat = lstat_relative(directory_ref, name)
    if not stat.S_ISREG(path_stat.st_mode):
        msg = f"{path} is not a regular file"
        raise OSError(msg)
    if deletable and sys.platform == "win32":  # pragma: win32 cover
        # A claim's owner must be able to unlink it while another process reads it.
        fd = open_deletable_for_reading(str(path))
    elif sys.platform == "win32":  # pragma: win32 cover
        # The sentinel stays for the lock's lifetime, so its handle shares no delete.
        fd = open_read_without_oplock_wait(str(path), share_delete=False)
    else:  # pragma: win32 no cover
        flags = os.O_RDONLY | O_BINARY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        fd = open_relative(directory_ref, name, flags, 0)
    try:
        record = _read_opened_record(fd, path, path_stat, limit)
    except BaseException as read_error:  # preserve read and descriptor cleanup errors
        try:
            os.close(fd)
        except BaseException as close_error:  # ruff:ignore[blind-except]  # preserve read and descriptor cleanup errors
            raise_cleanup_errors("strict record read cleanup failed", read_error, close_error)
        raise
    return fd, record


def lstat_relative(directory_ref: tuple[str, int | None], name: str) -> os.stat_result:
    directory, directory_fd = directory_ref
    if directory_fd is not None and SUPPORTS.stat_dir_fd:  # pragma: needs dir-fd
        return os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    return Path(directory, name).lstat()


def open_relative(directory_ref: tuple[str, int | None], name: str, flags: int, mode: int) -> int:
    directory, directory_fd = directory_ref
    return (
        os.open(name, flags, mode, dir_fd=directory_fd)
        if directory_fd is not None
        else os.open(Path(directory, name), flags, mode)
    )


def _read_opened_record(fd: int, path: Path, path_stat: os.stat_result, limit: int) -> bytes:
    opened_stat = os.fstat(fd)
    if not stat.S_ISREG(opened_stat.st_mode):
        msg = f"{path} is not a regular file"
        raise OSError(msg)
    if file_identity(opened_stat) != file_identity(path_stat):
        msg = f"{path} changed while opening"
        raise OSError(msg)
    record = os.read(fd, limit + 1)
    if len(record) > limit:
        msg = f"{path} exceeds {limit} bytes"
        raise OSError(msg)
    return record


def file_identity(st: os.stat_result) -> tuple[int, int]:
    return st.st_dev, st.st_ino


def link_or_find_committed(
    directory_ref: tuple[str, int | None],
    source_name: str,
    destination_name: str,
    source_identity: tuple[int, int] | None = None,
) -> None:
    try:
        _link_relative(directory_ref, source_name, destination_name)
    except FileExistsError:
        # An NFS retry can return EEXIST after a successful link. Its source name may already have been reclaimed.
        if (identity := relative_identity(directory_ref, destination_name)) is None or identity != (
            source_identity or relative_identity(directory_ref, source_name)
        ):
            raise


def _link_relative(directory_ref: tuple[str, int | None], source_name: str, destination_name: str) -> None:
    directory, directory_fd = directory_ref
    if directory_fd is not None and SUPPORTS.link_dir_fd:  # pragma: needs dir-fd
        link_no_follow(source_name, destination_name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
        return
    link_no_follow(Path(directory, source_name), Path(directory, destination_name))  # pragma: win32 cover


def link_no_follow(
    source: str | Path,
    destination: str | Path,
    *,
    src_dir_fd: int | None = None,
    dst_dir_fd: int | None = None,
) -> None:
    """
    Link a claim without following its source when the platform supports it.

    :param source: existing claim path
    :param destination: new claim path
    :param src_dir_fd: directory for a relative source
    :param dst_dir_fd: directory for a relative destination
    """
    if not SUPPORTS.has_link:
        msg = "os.link is unavailable on this platform"
        raise NotImplementedError(msg)
    if _LINK_HONORS_FOLLOW_SYMLINKS:
        os.link(source, destination, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd, follow_symlinks=False)
    else:  # pragma: lacks link-follow-symlinks
        os.link(source, destination, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd)


def relative_identity(directory_ref: tuple[str, int | None], name: str) -> tuple[int, int] | None:
    directory, directory_fd = directory_ref
    try:
        if directory_fd is not None and SUPPORTS.stat_dir_fd:  # pragma: needs dir-fd
            path_stat = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        else:  # pragma: win32 cover
            path_stat = Path(directory, name).lstat()
    except FileNotFoundError:
        return None
    return file_identity(path_stat)


def unlink_relative(directory_ref: tuple[str, int | None], name: str) -> None:
    directory, directory_fd = directory_ref
    if directory_fd is not None and SUPPORTS.unlink_dir_fd:  # pragma: needs dir-fd
        os.unlink(name, dir_fd=directory_fd)
    elif sys.platform == "win32":  # pragma: win32 cover
        unlink_in_directory(Path(directory), name)
    else:
        Path(directory, name).unlink()  # pragma: win32 no cover


def unlink_in_directory(
    directory: Path,
    name: str,
    *,
    claim_fd: int | None = None,
    cancel_wait: contextlib.AbstractContextManager[None] | None = None,
) -> BaseException | None:
    if claim_fd is not None:  # pragma: win32 cover
        return mark_claim_for_deletion(
            claim_fd,
            str(directory / name),
            lambda: str(directory / private_record_name(name)),
            cancel_wait=cancel_wait,
        )
    if SUPPORTS.unlink_dir_fd:  # pragma: needs dir-fd
        directory_fd = open_claim_directory(str(directory))
        try:
            os.unlink(name, dir_fd=directory_fd)
        except BaseException as unlink_error:  # preserve unlink and directory cleanup errors
            try:
                os.close(directory_fd)
            except BaseException as close_error:  # ruff:ignore[blind-except]  # preserve unlink and directory cleanup errors
                raise_cleanup_errors("strict unlink directory cleanup failed", unlink_error, close_error)
            raise
        try:
            os.close(directory_fd)
        except BaseException as close_error:  # ruff:ignore[blind-except]  # caller commits the removed path before raising
            return close_error
        return None
    if sys.platform != "win32":  # pragma: win32 no cover
        Path(directory / name).unlink()
        return None
    retry_delay = 0.001  # pragma: win32 cover
    max_retries: Final[int] = 10  # pragma: win32 cover
    for attempt in range(max_retries):  # pragma: win32 cover
        if (error := _unlink_error(directory / name)) is None:
            return None
        if error.errno not in {EACCES, EPERM} or attempt == max_retries - 1:
            raise error
        time.sleep(retry_delay)
        retry_delay *= 2
    return None  # pragma: no cover  # the final retry returns or raises


def open_claim_directory(directory: str) -> int:  # pragma: needs dir-fd
    # Opening both directories through descriptors keeps O_NOFOLLOW effective on both components.
    coordination_directory, name = os.path.split(directory)
    coordination_fd = open_directory(coordination_directory)
    try:
        return open_directory(name, coordination_fd)
    finally:
        # The walk wrote nothing through it, and an error here would leak the descriptor this function returns.
        with contextlib.suppress(OSError):
            os.close(coordination_fd)


def open_directory(directory: str, dir_fd: int | None = None) -> int:  # pragma: needs dir-fd
    return os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0), dir_fd=dir_fd)


def _unlink_error(path: Path) -> OSError | None:  # pragma: win32 cover
    try:
        path.unlink()
    except OSError as error:
        return error
    return None


def raise_if_hard_links_unsupported(lock_file: str, error: NotImplementedError | OSError) -> None:
    """
    Translate hard-link support errors from a claim publication.

    :param lock_file: lock path to identify in the exception
    :param error: exception raised by the hard-link operation
    :raises SoftFileLockProtocolError: the filesystem cannot publish a claim atomically
    """
    windows_hard_link_unsupported: Final[frozenset[int]] = frozenset({1, 17, 50})
    unsupported = (
        isinstance(error, NotImplementedError)
        or error.errno in _HARD_LINK_UNSUPPORTED_ERRNOS
        or (getattr(error, "winerror", None) in windows_hard_link_unsupported)
    )
    if unsupported:
        reason = "filesystem does not support atomic no-replace hard-link publication"
        raise SoftFileLockProtocolError(lock_file, None, reason) from error


def pin_claim_directory(directory: Path, *, acquiring: bool = False) -> contextlib.ExitStack[bool | None]:
    return pin_directories(directory.parent, directory, acquiring=acquiring)


def pin_directories(*directories: Path, acquiring: bool = False) -> contextlib.ExitStack[bool | None]:
    """
    Keep Windows directories from being replaced by junctions during an operation.

    :param directories: directories to pin for the operation
    :param acquiring: retry acquisition if an oplock blocks a pin
    :returns: a context manager that releases the pins
    :raises RetryDoorwayError: acquisition could not pin a directory
    """
    stack: contextlib.ExitStack[bool | None] = contextlib.ExitStack()
    if sys.platform == "win32":  # pragma: win32 cover
        with stack:
            for directory in directories:
                try:
                    pin = pin_directory(str(directory), wait_for_oplock=not acquiring)
                except DirectoryPinBlockedError as error:
                    raise RetryDoorwayError from error
                stack.callback(unpin_directory, pin)
            return stack.pop_all()
    return stack  # pragma: win32 no cover


def scandir_relative(
    directory_ref: tuple[str, int | None],
) -> contextlib.AbstractContextManager[Iterator[os.DirEntry[str]]]:
    directory, directory_fd = directory_ref
    if directory_fd is not None and SUPPORTS.scandir_fd:  # pragma: needs dir-fd
        return os.scandir(directory_fd)
    return os.scandir(directory)  # pragma: lacks dir-fd


__all__ = [
    "O_BINARY",
    "SUPPORTS",
    "file_identity",
    "link_no_follow",
    "link_or_find_committed",
    "lstat_relative",
    "open_claim_directory",
    "open_directory",
    "open_record",
    "open_relative",
    "pin_claim_directory",
    "pin_directories",
    "raise_if_hard_links_unsupported",
    "relative_identity",
    "scandir_relative",
    "unlink_in_directory",
    "unlink_private_record_once",
    "unlink_relative",
]
