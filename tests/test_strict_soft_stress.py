from __future__ import annotations

import os
import signal
import subprocess  # ruff:ignore[suspicious-subprocess-import]  # isolated interpreters exercise cross-process exclusion
import sys
import time
from typing import TYPE_CHECKING, Final

import pytest

if TYPE_CHECKING:
    from pathlib import Path

_PROCESS_COUNT: Final[int] = 8
_ACQUISITIONS_PER_PROCESS: Final[int] = 500
# Detect overlap by a lost update rather than an O_EXCL create: two holders in the critical section read the same
# count and one increment vanishes, so the total falls short. A create/unlink pair instead tripped over Windows'
# delete-pending lag, where the previous holder's unlink had not finished before the next holder's O_EXCL create ran.
_WORKER: Final[str] = """
import sys
import time
from pathlib import Path

from filelock import StrictSoftFileLock

lock_path, start_path, counter_path = map(Path, sys.argv[1:4])
while not start_path.exists():
    time.sleep(0.001)
for _ in range(int(sys.argv[4])):
    with StrictSoftFileLock(lock_path, timeout=30, poll_interval=0.0005):
        count = int(counter_path.read_text()) if counter_path.exists() else 0
        counter_path.write_text(str(count + 1))
"""


pytestmark = pytest.mark.requires_hard_links


@pytest.mark.timeout(90)
def test_strict_soft_eight_process_contention_has_no_overlap(tmp_path: Path) -> None:
    lock_path = tmp_path / "resource.lock"
    start_path = tmp_path / "start"
    counter_path = tmp_path / "counter"
    processes = [
        subprocess.Popen(
            [
                sys.executable,
                "-c",
                _WORKER,
                str(lock_path),
                str(start_path),
                str(counter_path),
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

    recorded = int(counter_path.read_text()) if counter_path.exists() else 0
    assert ([process.returncode for process in processes], outputs, recorded) == (
        [0] * _PROCESS_COUNT,
        [("", "")] * _PROCESS_COUNT,
        _PROCESS_COUNT * _ACQUISITIONS_PER_PROCESS,
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
