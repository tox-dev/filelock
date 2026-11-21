from __future__ import annotations

__lazy_modules__ = ["typing"]

import sys

if sys.platform == "win32":  # pragma: win32 cover
    import ctypes
    from ctypes import wintypes
    from typing import Final

    GENERIC_READ: Final[int] = 0x80000000
    GENERIC_WRITE: Final[int] = 0x40000000
    SYNCHRONIZE: Final[int] = 0x00100000
    DELETE: Final[int] = 0x00010000
    DESIRED_ACCESS: Final[int] = GENERIC_READ | GENERIC_WRITE | SYNCHRONIZE
    FILE_SHARE_READ_WRITE: Final[int] = 0x00000001 | 0x00000002  # Match the CRT sharing mode used by os.open.
    FILE_OPEN: Final[int] = 1
    FILE_CREATE: Final[int] = 2
    FILE_OPEN_IF: Final[int] = 3
    FILE_READ_ATTRIBUTES: Final[int] = 0x00000080
    FILE_SHARE_ALL: Final[int] = 0x00000001 | 0x00000002 | 0x00000004
    FILE_ATTRIBUTE_READONLY: Final[int] = 0x00000001
    FILE_ATTRIBUTE_NORMAL: Final[int] = 0x00000080
    FILE_ATTRIBUTE_REPARSE_POINT: Final[int] = 0x00000400
    FILE_DISPOSITION_INFO_CLASS: Final[int] = 4
    FILE_DISPOSITION_INFO_EX: Final[int] = 21
    DELETE_POSIX: Final[int] = 0x01 | 0x02  # DELETE | POSIX_SEMANTICS
    FILE_SYNCHRONOUS_IO_NONALERT: Final[int] = 0x00000020
    FILE_NON_DIRECTORY_FILE: Final[int] = 0x00000040
    FILE_DELETE_ON_CLOSE: Final[int] = 0x00001000
    FILE_COMPLETE_IF_OPLOCKED: Final[int] = 0x00000100
    FILE_OPEN_REPARSE_POINT: Final[int] = 0x00200000
    CREATE_OPTIONS: Final[int] = (
        FILE_SYNCHRONOUS_IO_NONALERT | FILE_NON_DIRECTORY_FILE | FILE_COMPLETE_IF_OPLOCKED | FILE_OPEN_REPARSE_POINT
    )
    FILE_LIST_DIRECTORY: Final[int] = 0x00000001
    FILE_DIRECTORY_FILE: Final[int] = 0x00000001
    DIRECTORY_OPTIONS: Final[int] = FILE_SYNCHRONOUS_IO_NONALERT | FILE_DIRECTORY_FILE | FILE_OPEN_REPARSE_POINT
    OBJ_CASE_INSENSITIVE: Final[int] = 0x00000040  # Win32 name lookups are case-insensitive
    OWNER_WRITE: Final[int] = 0o200
    INVALID_HANDLE_VALUE: Final[int | None] = ctypes.c_void_p(-1).value
    ERROR_ACCESS_DENIED: Final[int] = 5
    ERROR_OPERATION_ABORTED: Final[int] = 995
    DUPLICATE_SAME_ACCESS: Final[int] = 0x00000002
    STATUS_SUCCESS: Final[int] = 0x00000000
    STATUS_OPLOCK_BREAK_IN_PROGRESS: Final[int] = 0x00000108
    STATUS_ACCESS_DENIED: Final[int] = 0xC0000022
    STATUS_OBJECT_NAME_NOT_FOUND: Final[int] = 0xC0000034
    STATUS_OBJECT_NAME_COLLISION: Final[int] = 0xC0000035
    STATUS_OBJECT_PATH_NOT_FOUND: Final[int] = 0xC000003A
    STATUS_SHARING_VIOLATION: Final[int] = 0xC0000043
    STATUS_DELETE_PENDING: Final[int] = 0xC0000056
    NTDLL: Final[ctypes.WinDLL] = ctypes.WinDLL("ntdll")

    KERNEL32: Final[ctypes.WinDLL] = ctypes.WinDLL("kernel32", use_last_error=True)

    ADVAPI32: Final[ctypes.WinDLL] = ctypes.WinDLL("advapi32", use_last_error=True)

    class UnicodeString(ctypes.Structure):
        _fields_ = (
            ("Length", wintypes.USHORT),  # byte length, not character count
            ("MaximumLength", wintypes.USHORT),
            ("Buffer", wintypes.LPWSTR),
        )

    class ObjectAttributes(ctypes.Structure):
        _fields_ = (
            ("Length", wintypes.ULONG),
            ("RootDirectory", wintypes.HANDLE),
            ("ObjectName", ctypes.POINTER(UnicodeString)),
            ("Attributes", wintypes.ULONG),
            ("SecurityDescriptor", ctypes.c_void_p),
            ("SecurityQualityOfService", ctypes.c_void_p),
        )

    class IoStatusBlock(ctypes.Structure):
        _fields_ = (
            ("Status", ctypes.c_void_p),  # a union of NTSTATUS and PVOID, so it is pointer-sized
            ("Information", ctypes.c_void_p),
        )

    class SecurityAttributes(ctypes.Structure):
        _fields_ = (
            ("nLength", wintypes.DWORD),
            ("lpSecurityDescriptor", ctypes.c_void_p),
            ("bInheritHandle", wintypes.BOOL),
        )

    class ByHandleFileInformation(ctypes.Structure):
        _fields_ = (
            ("dwFileAttributes", wintypes.DWORD),
            ("ftCreationTime", wintypes.FILETIME),
            ("ftLastAccessTime", wintypes.FILETIME),
            ("ftLastWriteTime", wintypes.FILETIME),
            ("dwVolumeSerialNumber", wintypes.DWORD),
            ("nFileSizeHigh", wintypes.DWORD),
            ("nFileSizeLow", wintypes.DWORD),
            ("nNumberOfLinks", wintypes.DWORD),
            ("nFileIndexHigh", wintypes.DWORD),
            ("nFileIndexLow", wintypes.DWORD),
        )

    class FileStandardInfo(ctypes.Structure):
        _fields_ = (
            ("AllocationSize", ctypes.c_longlong),
            ("EndOfFile", ctypes.c_longlong),
            ("NumberOfLinks", wintypes.DWORD),
            ("DeletePending", ctypes.c_ubyte),
            ("Directory", ctypes.c_ubyte),
        )

    class FileRenameInfo(ctypes.Structure):
        _fields_ = (
            ("ReplaceIfExists", wintypes.BOOL),
            ("RootDirectory", wintypes.HANDLE),
            ("FileNameLength", wintypes.DWORD),
            ("FileName", wintypes.WCHAR * 1),
        )

    NTDLL.NtCreateFile.restype = wintypes.LONG
    NTDLL.NtCreateFile.argtypes = [
        ctypes.POINTER(wintypes.HANDLE),
        wintypes.DWORD,
        ctypes.POINTER(ObjectAttributes),
        ctypes.POINTER(IoStatusBlock),
        ctypes.POINTER(ctypes.c_longlong),
        wintypes.ULONG,
        wintypes.ULONG,
        wintypes.ULONG,
        wintypes.ULONG,
        ctypes.c_void_p,
        wintypes.ULONG,
    ]
    NTDLL.RtlDosPathNameToNtPathName_U_WithStatus.restype = wintypes.LONG
    NTDLL.RtlDosPathNameToNtPathName_U_WithStatus.argtypes = [
        wintypes.LPCWSTR,
        ctypes.POINTER(UnicodeString),
        ctypes.c_void_p,
        ctypes.c_void_p,
    ]
    NTDLL.RtlFreeUnicodeString.restype = None
    NTDLL.RtlFreeUnicodeString.argtypes = [ctypes.POINTER(UnicodeString)]
    NTDLL.RtlNtStatusToDosError.restype = wintypes.ULONG
    NTDLL.RtlNtStatusToDosError.argtypes = [wintypes.LONG]
    KERNEL32.CloseHandle.argtypes = [wintypes.HANDLE]
    KERNEL32.CloseHandle.restype = wintypes.BOOL
    KERNEL32.DuplicateHandle.argtypes = [
        wintypes.HANDLE,
        wintypes.HANDLE,
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.HANDLE),
        wintypes.DWORD,
        wintypes.BOOL,
        wintypes.DWORD,
    ]
    KERNEL32.DuplicateHandle.restype = wintypes.BOOL
    KERNEL32.CancelSynchronousIo.argtypes = [wintypes.HANDLE]
    KERNEL32.CancelSynchronousIo.restype = wintypes.BOOL
    KERNEL32.GetFileInformationByHandle.argtypes = [wintypes.HANDLE, ctypes.POINTER(ByHandleFileInformation)]
    KERNEL32.GetFileInformationByHandle.restype = wintypes.BOOL
    KERNEL32.GetFileInformationByHandleEx.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    KERNEL32.GetFileInformationByHandleEx.restype = wintypes.BOOL
    KERNEL32.WriteFile.argtypes = [
        wintypes.HANDLE,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        ctypes.c_void_p,
    ]
    KERNEL32.WriteFile.restype = wintypes.BOOL
    KERNEL32.ReOpenFile.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD]
    KERNEL32.ReOpenFile.restype = wintypes.HANDLE
    KERNEL32.SetFileInformationByHandle.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    KERNEL32.SetFileInformationByHandle.restype = wintypes.BOOL
    ADVAPI32.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(wintypes.ULONG),
    ]
    ADVAPI32.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = wintypes.BOOL
    ADVAPI32.GetNamedSecurityInfoW.argtypes = [
        wintypes.LPCWSTR,
        ctypes.c_int,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
    ]
    ADVAPI32.GetNamedSecurityInfoW.restype = wintypes.DWORD
    ADVAPI32.OpenThreadToken.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.BOOL,
        ctypes.POINTER(wintypes.HANDLE),
    ]
    ADVAPI32.OpenThreadToken.restype = wintypes.BOOL
    ADVAPI32.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    ADVAPI32.OpenProcessToken.restype = wintypes.BOOL
    ADVAPI32.GetTokenInformation.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    ADVAPI32.GetTokenInformation.restype = wintypes.BOOL
    KERNEL32.GetCurrentProcess.restype = wintypes.HANDLE
    KERNEL32.GetCurrentThread.restype = wintypes.HANDLE
    KERNEL32.LocalFree.argtypes = [ctypes.c_void_p]
    KERNEL32.LocalFree.restype = ctypes.c_void_p
    KERNEL32.CreateDirectoryW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(SecurityAttributes)]
    KERNEL32.CreateDirectoryW.restype = wintypes.BOOL
    KERNEL32.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    KERNEL32.CreateFileW.restype = ctypes.c_void_p

    __all__ = [
        "ADVAPI32",
        "CREATE_OPTIONS",
        "DELETE",
        "DELETE_POSIX",
        "DESIRED_ACCESS",
        "DIRECTORY_OPTIONS",
        "DUPLICATE_SAME_ACCESS",
        "ERROR_ACCESS_DENIED",
        "ERROR_OPERATION_ABORTED",
        "FILE_ATTRIBUTE_NORMAL",
        "FILE_ATTRIBUTE_READONLY",
        "FILE_ATTRIBUTE_REPARSE_POINT",
        "FILE_COMPLETE_IF_OPLOCKED",
        "FILE_CREATE",
        "FILE_DELETE_ON_CLOSE",
        "FILE_DISPOSITION_INFO_CLASS",
        "FILE_DISPOSITION_INFO_EX",
        "FILE_LIST_DIRECTORY",
        "FILE_OPEN",
        "FILE_OPEN_IF",
        "FILE_READ_ATTRIBUTES",
        "FILE_SHARE_ALL",
        "FILE_SHARE_READ_WRITE",
        "GENERIC_READ",
        "GENERIC_WRITE",
        "INVALID_HANDLE_VALUE",
        "KERNEL32",
        "NTDLL",
        "OBJ_CASE_INSENSITIVE",
        "OWNER_WRITE",
        "STATUS_ACCESS_DENIED",
        "STATUS_DELETE_PENDING",
        "STATUS_OBJECT_NAME_COLLISION",
        "STATUS_OBJECT_NAME_NOT_FOUND",
        "STATUS_OBJECT_PATH_NOT_FOUND",
        "STATUS_OPLOCK_BREAK_IN_PROGRESS",
        "STATUS_SHARING_VIOLATION",
        "STATUS_SUCCESS",
        "SYNCHRONIZE",
        "ByHandleFileInformation",
        "FileRenameInfo",
        "FileStandardInfo",
        "IoStatusBlock",
        "ObjectAttributes",
        "SecurityAttributes",
        "UnicodeString",
    ]
else:  # pragma: win32 no cover
    __all__ = []
