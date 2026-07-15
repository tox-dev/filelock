from __future__ import annotations

import os
import signal
import subprocess  # noqa: S404  # isolated interpreters exercise cross-process exclusion
import sys
import time
from typing import TYPE_CHECKING, Final

import pytest

if TYPE_CHECKING:
    from pathlib import Path

_PROCESS_COUNT: Final[int] = 8
_ACQUISITIONS_PER_PROCESS: Final[int] = 500
_WORKER: Final[str] = """
import os
import sys
import time
from pathlib import Path

from filelock import StrictSoftFileLock

lock_path, start_path, critical_path = map(Path, sys.argv[1:4])
while not start_path.exists():
    time.sleep(0.001)
for _ in range(int(sys.argv[4])):
    with StrictSoftFileLock(lock_path, timeout=30, poll_interval=0.0005):
        fd = os.open(critical_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(fd)
        critical_path.unlink()
"""


@pytest.mark.timeout(90)
def test_strict_soft_eight_process_contention_has_no_overlap(tmp_path: Path) -> None:
    lock_path = tmp_path / "resource.lock"
    start_path = tmp_path / "start"
    critical_path = tmp_path / "critical"
    processes = [
        subprocess.Popen(
            [
                sys.executable,
                "-c",
                _WORKER,
                str(lock_path),
                str(start_path),
                str(critical_path),
                str(_ACQUISITIONS_PER_PROCESS),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=sys.platform != "win32",
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0,
        )
        for _ in range(_PROCESS_COUNT)
    ]
    start_path.touch()
    deadline = time.monotonic() + 75
    outputs: list[tuple[str, str]] = []
    try:
        outputs.extend(process.communicate(timeout=max(0.1, deadline - time.monotonic())) for process in processes)
    except subprocess.TimeoutExpired:  # pragma: no cover - test failure cleanup
        _kill_processes(processes)
        pytest.fail("strict soft-lock stress workers exceeded 75 seconds")

    assert ([process.returncode for process in processes], outputs, critical_path.exists()) == (
        [0] * _PROCESS_COUNT,
        [("", "")] * _PROCESS_COUNT,
        False,
    )


def _kill_processes(processes: list[subprocess.Popen[str]]) -> None:  # pragma: no cover - test failure cleanup
    for process in processes:
        if process.poll() is None:
            if sys.platform == "win32":
                process.kill()
            else:
                os.killpg(process.pid, signal.SIGKILL)
    for process in processes:
        process.communicate()
