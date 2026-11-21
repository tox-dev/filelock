from __future__ import annotations

__lazy_modules__ = ["ctypes", "msvcrt", "typing"]

import sys

if sys.platform == "win32":  # pragma: win32 cover
    import ctypes
    import msvcrt
    import os
    import time
    from ctypes import wintypes
    from errno import ENOENT, EWOULDBLOCK
    from functools import cache
    from typing import Final, cast

    from ._bindings import (
        ADVAPI32,
        CREATE_OPTIONS,
        DELETE,
        DELETE_POSIX,
        DESIRED_ACCESS,
        ERROR_ACCESS_DENIED,
        FILE_ATTRIBUTE_NORMAL,
        FILE_ATTRIBUTE_READONLY,
        FILE_ATTRIBUTE_REPARSE_POINT,
        FILE_CREATE,
        FILE_DISPOSITION_INFO_CLASS,
        FILE_DISPOSITION_INFO_EX,
        FILE_OPEN,
        FILE_OPEN_IF,
        FILE_READ_ATTRIBUTES,
        FILE_SHARE_ALL,
        FILE_SHARE_READ_WRITE,
        GENERIC_READ,
        INVALID_HANDLE_VALUE,
        KERNEL32,
        NTDLL,
        OBJ_CASE_INSENSITIVE,
        OWNER_WRITE,
        STATUS_ACCESS_DENIED,
        STATUS_DELETE_PENDING,
        STATUS_OBJECT_NAME_NOT_FOUND,
        STATUS_OBJECT_PATH_NOT_FOUND,
        STATUS_OPLOCK_BREAK_IN_PROGRESS,
        STATUS_SHARING_VIOLATION,
        STATUS_SUCCESS,
        SYNCHRONIZE,
        ByHandleFileInformation,
        FileRenameInfo,
        FileStandardInfo,
        IoStatusBlock,
        ObjectAttributes,
        UnicodeString,
    )

    _ACCESS_DENIED_PROBES: Final[int] = 250
    _ACCESS_DENIED_RETRY: Final[float] = 0.002

    def open_non_reparse_fd(path: str, mode: int) -> int | None:
        """
        Open a lock file without following reparse points.

        :param path: lock file name
        :param mode: requested creation permissions
        :returns: read/write descriptor, or None for sharing violations and deletion in progress
        :raises BlockingIOError: an oplock break is in progress
        """
        # Checking the open handle avoids racing a reparse-point replacement. NtCreateFile also distinguishes
        # a name pending deletion from a permanent access denial.
        sys.audit("open", path, None, os.O_RDWR | os.O_CREAT)
        read_only = not mode & OWNER_WRITE
        owner_only = not mode & 0o077
        handle, status = _nt_open(path, read_only=read_only, owner_only=owner_only)
        for _ in range(_ACCESS_DENIED_PROBES):
            if status != STATUS_ACCESS_DENIED:
                break
            time.sleep(_ACCESS_DENIED_RETRY)
            handle, status = _nt_open(path, read_only=read_only, owner_only=owner_only)
        if status == STATUS_OPLOCK_BREAK_IN_PROGRESS:
            msg = "oplock break in progress"
            raise BlockingIOError(EWOULDBLOCK, msg, path)
        if status != STATUS_SUCCESS:
            if status in {STATUS_SHARING_VIOLATION, STATUS_DELETE_PENDING}:
                return None
            raise status_error(status, path)

        return descriptor_for_non_reparse(handle, path, os.O_RDWR)

    def _nt_open(path: str, *, read_only: bool, owner_only: bool) -> tuple[int, int]:
        # Only newly created owner-only files need a private DACL.
        attributes = FILE_ATTRIBUTE_READONLY if read_only else FILE_ATTRIBUTE_NORMAL
        descriptor = owner_only_descriptor() if owner_only else None
        return nt_create(path, DESIRED_ACCESS, attributes, FILE_SHARE_READ_WRITE, FILE_OPEN_IF, descriptor=descriptor)

    @cache
    def owner_only_descriptor() -> ctypes.c_void_p:
        descriptor = ctypes.c_void_p()
        if not ADVAPI32.ConvertStringSecurityDescriptorToSecurityDescriptorW(
            "D:P(A;;FA;;;SY)(A;;FA;;;BA)(A;;FA;;;OW)", 1, ctypes.byref(descriptor), None
        ):  # pragma: no cover  # a constant string converts
            raise ctypes.WinError(ctypes.get_last_error())
        return descriptor

    def nt_create(  # ruff:ignore[too-many-arguments, too-many-positional-arguments]  # NtCreateFile's own knobs
        path: str,
        access: int,
        attributes: int,
        share: int,
        disposition: int,
        options: int = CREATE_OPTIONS,
        *,
        descriptor: ctypes.c_void_p | None = None,
        raw: wintypes.HANDLE | None = None,
    ) -> tuple[int, int]:
        nt_name = UnicodeString()
        status = NTDLL.RtlDosPathNameToNtPathName_U_WithStatus(path, ctypes.byref(nt_name), None, None) & 0xFFFFFFFF
        if status != STATUS_SUCCESS:
            return 0, status
        try:
            object_attributes = ObjectAttributes()
            object_attributes.Length = ctypes.sizeof(ObjectAttributes)
            object_attributes.ObjectName = ctypes.pointer(nt_name)
            object_attributes.Attributes = OBJ_CASE_INSENSITIVE
            object_attributes.SecurityDescriptor = descriptor
            handle = wintypes.HANDLE() if raw is None else raw
            io_status = IoStatusBlock()
            status = (
                NTDLL.NtCreateFile(
                    ctypes.byref(handle),
                    access,
                    ctypes.byref(object_attributes),
                    ctypes.byref(io_status),
                    None,
                    attributes,
                    share,
                    disposition,
                    options,
                    None,
                    0,
                )
                & 0xFFFFFFFF
            )
        finally:
            NTDLL.RtlFreeUnicodeString(ctypes.byref(nt_name))
        # An exclusive create keeps the raw handle until descriptor conversion or rollback.
        if status == STATUS_OPLOCK_BREAK_IN_PROGRESS and raw is None:
            KERNEL32.CloseHandle(handle)
        if status != STATUS_SUCCESS:
            return 0, status
        return handle.value or 0, status

    def status_error(status: int, path: str) -> OSError:
        if status == STATUS_OPLOCK_BREAK_IN_PROGRESS:
            return BlockingIOError(EWOULDBLOCK, "oplock break in progress", path)
        if status in {STATUS_OBJECT_NAME_NOT_FOUND, STATUS_OBJECT_PATH_NOT_FOUND}:
            return FileNotFoundError(ENOENT, os.strerror(ENOENT), path)
        winerror = NTDLL.RtlNtStatusToDosError(status)
        return OSError(None, ctypes.FormatError(winerror).strip(), path, winerror)

    def descriptor_for_non_reparse(handle: int, path: str, flags: int) -> int:
        info = ByHandleFileInformation()
        # The handle was just opened with query access.
        if not KERNEL32.GetFileInformationByHandle(handle, ctypes.byref(info)):  # pragma: no cover
            err = ctypes.get_last_error()
            KERNEL32.CloseHandle(handle)
            raise ctypes.WinError(err)
        if info.dwFileAttributes & FILE_ATTRIBUTE_REPARSE_POINT:  # pragma: needs symlink
            KERNEL32.CloseHandle(handle)
            msg = f"Lock file is a reparse point (symlink/junction): {path}"
            raise OSError(msg)

        try:
            # Child processes must not inherit the lock handle.
            return msvcrt.open_osfhandle(handle, flags | os.O_NOINHERIT)
        except BaseException:  # pragma: no cover  # open_osfhandle audits too; a hook raising must not leak the handle
            KERNEL32.CloseHandle(handle)
            raise

    def open_deletable_for_reading(path: str) -> int:
        """
        Read a claim without blocking its deletion.

        :param path: claim file name
        :returns: read-only descriptor
        :raises OSError: Windows refused the open
        """
        sys.audit("open", path, None, os.O_RDONLY)
        handle, status = nt_create(path, GENERIC_READ | SYNCHRONIZE, 0, FILE_SHARE_ALL, FILE_OPEN)
        if status != STATUS_SUCCESS:
            winerror = NTDLL.RtlNtStatusToDosError(status)
            raise OSError(None, ctypes.FormatError(winerror).strip(), path, winerror)
        try:
            return msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_NOINHERIT)
        except BaseException:  # pragma: no cover  # open_osfhandle audits too; a hook raising must not leak the handle
            KERNEL32.CloseHandle(handle)
            raise

    def is_unlinked(path: str) -> bool:
        """
        Check whether the name is absent or pending deletion.

        :param path: name to inspect
        :returns: whether the file was unlinked
        """
        # NTFS can keep a deleted name until the last reader closes it.
        handle, status = nt_create(path, FILE_READ_ATTRIBUTES | SYNCHRONIZE, 0, FILE_SHARE_ALL, FILE_OPEN)
        if handle:
            KERNEL32.CloseHandle(handle)
        return status in {STATUS_OBJECT_NAME_NOT_FOUND, STATUS_DELETE_PENDING}

    def identity_at(path: str) -> tuple[int, int]:
        """
        Identify a file or directory without following a reparse point.

        :param path: name to inspect
        :returns: volume serial number and file index
        :raises WinError: opening the name failed
        """
        handle = KERNEL32.CreateFileW(
            path,
            FILE_READ_ATTRIBUTES,
            FILE_SHARE_ALL,
            None,
            3,  # OPEN_EXISTING
            0x02000000 | 0x00200000,  # FILE_FLAG_BACKUP_SEMANTICS opens a directory; FILE_FLAG_OPEN_REPARSE_POINT
            None,
        )
        if handle in {
            None,
            INVALID_HANDLE_VALUE,
        }:  # pragma: no cover  # callers open a name they just made or listed
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            return identity_of_handle(cast("int", handle))
        finally:
            KERNEL32.CloseHandle(handle)

    def identity_of_fd(fd: int) -> tuple[int, int]:
        """
        Identify the file held by a descriptor.

        :param fd: open file descriptor
        :returns: volume serial number and file index
        """
        return identity_of_handle(msvcrt.get_osfhandle(fd))

    def identity_of_handle(handle: int) -> tuple[int, int]:
        info = ByHandleFileInformation()
        # The handle was just opened with query access.
        if not KERNEL32.GetFileInformationByHandle(handle, ctypes.byref(info)):  # pragma: no cover
            raise ctypes.WinError(ctypes.get_last_error())
        return info.dwVolumeSerialNumber, info.nFileIndexHigh << 32 | info.nFileIndexLow

    def mark_delete_disposition(handle: int, *, published: bool) -> None:
        """
        Remove a published name or schedule a private file for deletion on close.

        :param handle: Windows handle with delete access
        :param published: request POSIX deletion for a published claim
        :raises WinError: Windows refused the deletion
        """
        if try_set_delete_disposition(handle, published=published):
            return
        error = ctypes.get_last_error()
        if error == ERROR_ACCESS_DENIED:
            # A peer may already have deleted this file through another handle.
            info = FileStandardInfo()
            if KERNEL32.GetFileInformationByHandleEx(handle, 1, ctypes.byref(info), ctypes.sizeof(info)) and (
                info.DeletePending
            ):
                return
        raise ctypes.WinError(error)

    def try_set_delete_disposition(handle: int, *, published: bool = False) -> bool:
        disposition = wintypes.DWORD(DELETE_POSIX) if published else ctypes.c_byte(1)
        return bool(
            KERNEL32.SetFileInformationByHandle(
                handle,
                FILE_DISPOSITION_INFO_EX if published else FILE_DISPOSITION_INFO_CLASS,
                ctypes.byref(disposition),
                ctypes.sizeof(disposition),
            )
        )

    def rename_open_file(fd: int, public_path: str) -> None:
        """
        Publish the open file without overwriting another claim.

        :param fd: descriptor of the private file
        :param public_path: destination name
        :raises WinError: the rename failed
        """
        name = public_path.encode("utf-16-le")
        buffer = ctypes.create_string_buffer(ctypes.sizeof(FileRenameInfo) + len(name))
        FileRenameInfo.from_buffer(buffer).FileNameLength = len(name)
        ctypes.memmove(ctypes.addressof(buffer) + FileRenameInfo.FileName.offset, name, len(name))
        if not KERNEL32.SetFileInformationByHandle(msvcrt.get_osfhandle(fd), 3, buffer, len(buffer)):
            raise ctypes.WinError(ctypes.get_last_error())

    def create_deletable_fd(path: str) -> int:
        """
        Create a file with delete access for publication and cleanup.

        :param path: private claim name
        :returns: read/write descriptor
        """
        handle, status = nt_create(path, DESIRED_ACCESS | DELETE, FILE_ATTRIBUTE_NORMAL, FILE_SHARE_ALL, FILE_CREATE)
        if status != STATUS_SUCCESS:
            raise status_error(status, path)
        return descriptor_for_non_reparse(handle, path, os.O_RDWR | os.O_BINARY)

    __all__ = [
        "create_deletable_fd",
        "descriptor_for_non_reparse",
        "identity_at",
        "identity_of_fd",
        "identity_of_handle",
        "is_unlinked",
        "mark_delete_disposition",
        "nt_create",
        "open_deletable_for_reading",
        "open_non_reparse_fd",
        "owner_only_descriptor",
        "rename_open_file",
        "status_error",
        "try_set_delete_disposition",
    ]
else:  # pragma: win32 no cover
    __all__ = []
