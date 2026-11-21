from __future__ import annotations

import subprocess  # ruff:ignore[suspicious-subprocess-import]  # runs this test's own interpreter
import sys
import threading
from typing import TYPE_CHECKING, Final

import pytest

from filelock import SoftReadWriteLock, Timeout

from .lock_helpers import PROCESS_DEADLINE, make_lock

if TYPE_CHECKING:
    from pathlib import Path

    from pytest_mock import MockerFixture

pytestmark: Final[list[pytest.MarkDecorator]] = [
    pytest.mark.requires_hard_links,
    pytest.mark.usefixtures("clear_singletons"),
]


@pytest.mark.parametrize("owner", ["main", "finished-reader", "finished-writer", "idle-daemon", "finished-loop"])
def test_atexit_releases_held_lock(lock_file: str, tmp_path: Path, owner: str) -> None:
    script = """
import atexit
import asyncio
import sys
from pathlib import Path
import threading

def check_released():
    from filelock import SoftReadWriteLock

    peer = SoftReadWriteLock(sys.argv[1], is_singleton=False)
    peer.acquire_write(timeout=0)
    peer.release()
    Path(sys.argv[2]).write_text("released")

atexit.register(check_released)

from filelock import AsyncSoftReadWriteLock, SoftReadWriteLock

lock = SoftReadWriteLock(sys.argv[1], is_singleton=False)
owner = sys.argv[3]
if owner in {"finished-reader", "finished-writer"}:
    acquire = lock.acquire_read if owner == "finished-reader" else lock.acquire_write
    held = threading.Event()
    def take_lock():
        acquire(timeout=0)
        held.set()
    thread = threading.Thread(target=take_lock)
    thread.start()
    thread.join()
    assert held.is_set()
elif owner == "finished-loop":
    threading.Thread(target=threading.Event().wait, daemon=True).start()
    async_lock = AsyncSoftReadWriteLock(sys.argv[1])
    asyncio.run(async_lock.acquire_write(timeout=0))
else:
    if owner == "idle-daemon":
        threading.Thread(target=threading.Event().wait, daemon=True).start()
    lock.acquire_write(timeout=0)
"""
    result = subprocess.run(
        [sys.executable, "-c", script, lock_file, str(tmp_path / "released"), owner],
        check=False,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert (result.returncode, result.stderr) == (0, "")
    assert (tmp_path / "released").read_text() == "released"


@pytest.mark.parametrize("mode", ["read", "write", "async-read", "async-write", "async-joining"])
def test_daemon_hold_survives_interpreter_cleanup(lock_file: str, lock: SoftReadWriteLock, mode: str) -> None:
    script = """
import atexit
import asyncio
import sys
import threading

def linger():
    print("exiting", flush=True)
    sys.stdin.readline()

atexit.register(linger)

from filelock import AsyncSoftReadWriteLock, SoftReadWriteLock

lock = SoftReadWriteLock(sys.argv[1])
held = threading.Event()

def work_under_the_lock():
    with getattr(lock, sys.argv[2] + "_lock")():
        held.set()
        threading.Event().wait()

async def async_work():
    async_lock = AsyncSoftReadWriteLock(sys.argv[1])
    if sys.argv[2] == "async-joining":
        await async_lock.acquire_read()
        def check_joinable(self):
            held.set()
            threading.Event().wait()
        SoftReadWriteLock.compromise = property(check_joinable)
        await async_lock.acquire_read()
    else:
        async with getattr(async_lock, sys.argv[2].removeprefix("async-") + "_lock")():
            held.set()
            await asyncio.Event().wait()

work = (lambda: asyncio.run(async_work())) if sys.argv[2].startswith("async-") else work_under_the_lock
threading.Thread(target=work, daemon=True).start()
held.wait()
"""
    holder = subprocess.Popen(
        [sys.executable, "-c", script, lock_file, mode],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout is not None
        assert holder.stdout.readline() == "exiting\n"
        with pytest.raises(Timeout):
            lock.acquire_write(blocking=False)
    finally:
        holder.communicate("\n", timeout=PROCESS_DEADLINE)

    assert holder.returncode == 0
    with pytest.raises(Timeout):
        lock.acquire_write(blocking=False)


def test_release_interrupted_while_joining_the_heartbeat_still_leaves(lock_file: str, mocker: MockerFixture) -> None:
    lock = make_lock(lock_file)
    lock.acquire_write(timeout=2)
    mocker.patch.object(threading.Thread, "join", autospec=True, side_effect=KeyboardInterrupt)
    with pytest.raises(KeyboardInterrupt):
        lock.release()
    mocker.stopall()
    peer = make_lock(lock_file)
    with peer.write_lock(blocking=False):
        assert peer.generation is not None
    lock.close()
    peer.close()
