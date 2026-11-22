from __future__ import annotations

import subprocess  # ruff:ignore[suspicious-subprocess-import]  # only fixed-argument, no-shell runs below
import sys
from typing import TYPE_CHECKING, get_args

import filelock
import filelock.version

if TYPE_CHECKING:
    from pathlib import Path


def test_import_succeeds_without_os_link() -> None:
    assert _python_without_links("from filelock import FileLock, StrictSoftFileLock; print(FileLock.__name__)") == (
        filelock.FileLock.__name__
    )


def _python_without_links(script: str, *args: Path) -> str:
    # The backend snapshots os.link support at import, as on Termux.
    prefix = "import os, sys\nif hasattr(os, 'link'):\n    del os.link\n"
    result = subprocess.run([sys.executable, "-c", prefix + script, *args], capture_output=True, check=True, text=True)
    return result.stdout.strip()


def test_strict_lock_reports_unsupported_without_os_link(tmp_path: Path) -> None:
    result = _python_without_links(
        """from filelock import SoftFileLockProtocolError, StrictSoftFileLock
lock = StrictSoftFileLock(sys.argv[1])
try:
    lock.acquire()
except SoftFileLockProtocolError as error:
    print(error.reason, lock.claims, lock.is_locked)
""",
        tmp_path / "resource.lock",
    )
    assert "hard-link publication" in result
    assert result.endswith("() False")


def test_version_attribute_is_the_version_string() -> None:
    assert (type(filelock.version), filelock.version) == (str, filelock.__version__)


def test_has_fcntl_matches_native_unix_alias() -> None:
    assert filelock.has_fcntl is (filelock.FileLock is filelock.UnixFileLock)


def test_public_literal_aliases_list_their_values() -> None:
    assert (
        get_args(filelock.CloseErrorPolicy),
        get_args(filelock.ContextErrorPolicy),
        get_args(filelock.StrictSoftFileClaimState),
        get_args(filelock.OwnerMode),
        get_args(filelock.CompromiseReason),
    ) == (
        ("default", "raise", "suppress"),
        ("chain", "group"),
        ("intent", "held"),
        ("lease", "exclusive", "unknown"),
        ("marker-missing", "owner-changed", "refresh-failed", "evicted"),
    )


def test_context_resolves_wait_settings() -> None:
    context = filelock.FileLockContext("lock", timeout=2, mode=0o600, blocking=True, poll_interval=0.1)
    wait = context.acquire_wait(None, 0.2, None, blocking=None, cancel_check=None)
    assert isinstance(wait, filelock.AcquireWait)
    assert (wait.blocking, wait.timeout, wait.poll_interval) == (True, 2, 0.2)
