from __future__ import annotations

import os
import subprocess  # ruff:ignore[suspicious-subprocess-import]  # each client needs an isolated filelock installation
import sys
from typing import TYPE_CHECKING, Final, TextIO, cast

import pytest
from coverage_pragmas import CAPABILITIES

from filelock import StrictSoftFileLock, Timeout

if TYPE_CHECKING:
    from pathlib import Path

_OLDEST_LEGACY_VERSION: Final[str] = "3.20.0"
_OLD_CLIENT_PATH_VARIABLE: Final[str] = "FILELOCK_OLD_CLIENT_PATH"
_OLD_HOLDER: Final[str] = """
import sys

from filelock import SoftFileLock

lock = SoftFileLock(sys.argv[1])
lock.acquire(timeout=5)
print("acquired", flush=True)
sys.stdin.readline()
lock.release()
print("released", flush=True)
"""
_OLD_PROBE: Final[str] = """
import sys

from filelock import SoftFileLock, Timeout

lock = SoftFileLock(sys.argv[1])
try:
    lock.acquire(timeout=0)
except Timeout:
    print("blocked")
else:
    lock.release()
    print("acquired")
"""


pytestmark = [
    pytest.mark.requires_hard_links,
    pytest.mark.skipif(
        not CAPABILITIES["old-client"],
        reason=f"{_OLD_CLIENT_PATH_VARIABLE} must name an installed filelock {_OLDEST_LEGACY_VERSION}",
    ),
]


@pytest.fixture(scope="module")
def old_client_env() -> dict[str, str]:  # pragma: needs old-client
    old_client_path = os.environ[_OLD_CLIENT_PATH_VARIABLE]
    env = os.environ.copy()
    # Prepend: the inherited PYTHONPATH carries the coverage plugin this child restarts under.
    env["PYTHONPATH"] = os.pathsep.join(part for part in (old_client_path, env.get("PYTHONPATH")) if part)
    installed = subprocess.run(
        [sys.executable, "-c", "import filelock; print(filelock.__version__)"],
        check=True,
        capture_output=True,
        cwd=old_client_path,
        env=env,
        text=True,
    )
    assert installed.stdout.strip() == _OLDEST_LEGACY_VERSION
    return env


def test_old_soft_holder_blocks_strict_client(
    tmp_path: Path, old_client_env: dict[str, str]
) -> None:  # pragma: needs old-client
    lock_path = tmp_path / "resource.lock"
    with subprocess.Popen(
        [sys.executable, "-c", _OLD_HOLDER, os.fspath(lock_path)],
        cwd=tmp_path,
        env=old_client_env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    ) as holder:
        assert cast("TextIO", holder.stdout).readline() == "acquired\n"
        with pytest.raises(Timeout):
            StrictSoftFileLock(lock_path, timeout=0).acquire()
        stdout, stderr = holder.communicate("\n", timeout=5)
        assert (holder.returncode, stdout, stderr) == (0, "released\n", "")

    with StrictSoftFileLock(lock_path, timeout=0) as strict:
        assert strict.is_locked


def test_strict_activation_rejects_old_soft_client(
    tmp_path: Path, old_client_env: dict[str, str]
) -> None:  # pragma: needs old-client
    lock_path = tmp_path / "resource.lock"
    with StrictSoftFileLock(lock_path):
        blocked = subprocess.run(
            [sys.executable, "-c", _OLD_PROBE, os.fspath(lock_path)],
            check=True,
            capture_output=True,
            cwd=tmp_path,
            env=old_client_env,
            text=True,
        )
    after_release = subprocess.run(
        [sys.executable, "-c", _OLD_PROBE, os.fspath(lock_path)],
        check=True,
        capture_output=True,
        cwd=tmp_path,
        env=old_client_env,
        text=True,
    )

    assert (blocked.stdout, blocked.stderr, after_release.stdout, after_release.stderr) == (
        "blocked\n",
        "",
        "blocked\n",
        "",
    )
