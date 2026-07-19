"""Run a command on Windows with symbolic-link creation denied, and report why it is or is not denied.

Windows grants symlink creation through two independent paths, so closing one proves nothing. A process holding
SeCreateSymbolicLinkPrivilege may always create them; separately, Developer Mode lets an unprivileged process create
them through SYMBOLIC_LINK_FLAG_ALLOW_UNPRIVILEGED_CREATE. The CI job clears the Developer Mode registry value, and
this script removes the privilege from its own token.

SE_PRIVILEGE_REMOVED is documented as irreversible, with checks for a removed privilege returning
STATUS_PRIVILEGE_NOT_HELD, which is the guarantee this relies on. The documentation does not say whether a child
inherits the removal or whether clearing Developer Mode needs a reboot, so this reports what it observes rather
than assuming. Read those lines in the job log before trusting the job.

Usage: python deny_symlink.py <command> [args...]
"""

from __future__ import annotations

import ctypes
import subprocess  # ruff:ignore[suspicious-subprocess-import]  # launching the suite is this script's whole job
import sys
import tempfile
from ctypes import wintypes
from pathlib import Path
from typing import Final

_TOKEN_ADJUST_PRIVILEGES: Final[int] = 0x0020
_TOKEN_QUERY: Final[int] = 0x0008
_SE_PRIVILEGE_REMOVED: Final[int] = 0x0004
_ERROR_NOT_ALL_ASSIGNED: Final[int] = 1300
_PRIVILEGE: Final[str] = "SeCreateSymbolicLinkPrivilege"


class _Luid(ctypes.Structure):
    _fields_ = (("low_part", wintypes.DWORD), ("high_part", wintypes.LONG))


class _LuidAndAttributes(ctypes.Structure):
    _fields_ = (("luid", _Luid), ("attributes", wintypes.DWORD))


class _TokenPrivileges(ctypes.Structure):
    _fields_ = (("privilege_count", wintypes.DWORD), ("privileges", _LuidAndAttributes * 1))


def main() -> int:
    if sys.platform != "win32":
        print("deny_symlink only applies to Windows")
        return 1

    print(f"developer mode: {_developer_mode()}")
    print(f"{_PRIVILEGE} before: {_privilege_state()}")
    print(f"symlink before: {_can_symlink()}")

    print(f"removal reported: {_remove_privilege()}")
    print(f"{_PRIVILEGE} after: {_privilege_state()}")
    print(f"symlink after (this process): {_can_symlink()}")

    child = subprocess.run(
        [sys.executable, "-c", _CHILD_PROBE],
        capture_output=True,
        text=True,
        check=False,
    )
    print(f"symlink after (child process): {child.stdout.strip() or child.stderr.strip()}")

    return subprocess.run(sys.argv[1:], check=False).returncode


def _developer_mode() -> str:
    # ty needs the platform narrowed to see the Windows-only members from any host.
    assert sys.platform == "win32"
    import winreg  # ruff:ignore[import-outside-top-level]  Windows-only, and this module already refuses to run elsewhere

    try:
        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\AppModelUnlock"
        ) as key:
            return str(winreg.QueryValueEx(key, "AllowDevelopmentWithoutDevLicense")[0])
    except OSError as error:
        return f"unreadable ({error})"


def _privilege_state() -> str:
    result = subprocess.run(
        ["whoami", "/priv"],  # ruff:ignore[start-process-with-partial-path]  resolved from PATH on every Windows image
        capture_output=True,
        text=True,
        check=False,
    )
    for line in result.stdout.splitlines():
        if _PRIVILEGE in line:
            return line.strip()
    return "absent from the token"


def _remove_privilege() -> str:
    # ty needs the platform narrowed to see the Windows-only members from any host.
    assert sys.platform == "win32"
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    # Declare the signatures: GetCurrentProcess returns the pseudo-handle (HANDLE)-1, and ctypes' default c_int restype
    # truncates it on 64-bit, so OpenProcessToken is handed a bad handle and fails with ERROR_INVALID_HANDLE.
    kernel32.GetCurrentProcess.argtypes = ()
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    advapi32.OpenProcessToken.argtypes = (wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE))
    advapi32.OpenProcessToken.restype = wintypes.BOOL
    advapi32.LookupPrivilegeValueW.argtypes = (wintypes.LPCWSTR, wintypes.LPCWSTR, ctypes.POINTER(_Luid))
    advapi32.LookupPrivilegeValueW.restype = wintypes.BOOL
    advapi32.AdjustTokenPrivileges.argtypes = (
        wintypes.HANDLE,
        wintypes.BOOL,
        ctypes.POINTER(_TokenPrivileges),
        wintypes.DWORD,
        ctypes.c_void_p,
        ctypes.c_void_p,
    )
    advapi32.AdjustTokenPrivileges.restype = wintypes.BOOL

    token = wintypes.HANDLE()
    if not advapi32.OpenProcessToken(
        kernel32.GetCurrentProcess(), _TOKEN_ADJUST_PRIVILEGES | _TOKEN_QUERY, ctypes.byref(token)
    ):
        return f"OpenProcessToken failed with {ctypes.get_last_error()}"

    luid = _Luid()
    if not advapi32.LookupPrivilegeValueW(None, _PRIVILEGE, ctypes.byref(luid)):
        return f"LookupPrivilegeValue failed with {ctypes.get_last_error()}"

    privileges = _TokenPrivileges(1, (_LuidAndAttributes * 1)(_LuidAndAttributes(luid, _SE_PRIVILEGE_REMOVED)))
    if not advapi32.AdjustTokenPrivileges(token, False, ctypes.byref(privileges), 0, None, None):  # ruff:ignore[boolean-positional-value-in-call]
        return f"AdjustTokenPrivileges failed with {ctypes.get_last_error()}"
    # The call reports success even when the token never held the privilege, so read the code it leaves behind.
    if (code := ctypes.get_last_error()) == _ERROR_NOT_ALL_ASSIGNED:
        return "the token did not hold the privilege"
    return "removed" if code == 0 else f"unexpected code {code}"


_CHILD_PROBE: Final[str] = """
import pathlib, tempfile

with tempfile.TemporaryDirectory() as directory:
    target = pathlib.Path(directory, "target")
    target.touch()
    try:
        pathlib.Path(directory, "link").symlink_to(target)
    except OSError as error:
        print(f"False (WinError {error.winerror})")
    else:
        print("True")
"""


def _can_symlink() -> str:
    # ty needs the platform narrowed to see the Windows-only members from any host.
    assert sys.platform == "win32"
    with tempfile.TemporaryDirectory() as directory:
        target = Path(directory, "target")
        target.touch()
        try:
            Path(directory, "link").symlink_to(target)
        except OSError as error:
            return f"False (WinError {error.winerror})"
        return "True"


if __name__ == "__main__":
    raise SystemExit(main())
