from __future__ import annotations

import os
import socket
import time
from contextlib import closing
from pathlib import Path
from typing import TYPE_CHECKING, Final, Literal

import pytest

from filelock import AsyncSoftReadWriteLock, SoftReadWriteLock, Timeout

if TYPE_CHECKING:
    from pytest_mock import MockerFixture


@pytest.mark.parametrize("mode", [pytest.param("read", id="read"), pytest.param("write", id="write")])
@pytest.mark.parametrize(
    ("timeout", "blocking"),
    [
        pytest.param(0.02, True, id="deadline"),
        pytest.param(0, True, id="zero-timeout"),
        pytest.param(-1, False, id="nonblocking-unbounded"),
        pytest.param(10, False, id="nonblocking-finite"),
    ],
)
def test_state_contention_obeys_acquisition_policy(
    abandoned_state: Path, mocker: MockerFixture, mode: Literal["read", "write"], timeout: float, *, blocking: bool
) -> None:
    real_sleep: Final = time.sleep

    def sleep(seconds: float) -> None:
        assert blocking
        assert 0 < seconds <= timeout
        real_sleep(seconds)

    mocker.patch("time.sleep", autospec=True, side_effect=sleep)
    with closing(SoftReadWriteLock(abandoned_state, timeout=timeout, blocking=blocking, poll_interval=10)) as lock:
        acquire: Final = lock.acquire_read if mode == "read" else lock.acquire_write
        with pytest.raises(Timeout) as caught:
            acquire()
        assert caught.value.lock_file == str(abandoned_state)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", [pytest.param("read", id="read"), pytest.param("write", id="write")])
async def test_async_state_contention_reports_public_path(
    abandoned_state: Path, mode: Literal["read", "write"]
) -> None:
    lock: Final = AsyncSoftReadWriteLock(abandoned_state, timeout=0.02)
    try:
        acquire: Final = lock.acquire_read if mode == "read" else lock.acquire_write
        with pytest.raises(Timeout) as caught:
            await acquire()
        assert caught.value.lock_file == str(abandoned_state)
    finally:
        await lock.close()


@pytest.mark.parametrize("blocking", [pytest.param(True, id="deadline"), pytest.param(False, id="nonblocking")])
def test_writer_timeout_leaves_claim_if_state_is_busy(tmp_path: Path, mocker: MockerFixture, *, blocking: bool) -> None:
    path: Final = tmp_path / "test.lock"
    with (
        closing(SoftReadWriteLock(path, is_singleton=False, heartbeat_interval=10)) as reader,
        closing(SoftReadWriteLock(path, is_singleton=False, heartbeat_interval=10, poll_interval=0.01)) as writer,
    ):
        reader.acquire_read()
        real_unlink: Final = os.unlink
        state: Final = f"{path}.state"

        def unlink(filename: str | Path, *, dir_fd: int | None = None) -> None:
            real_unlink(filename, dir_fd=dir_fd)
            # A peer claims .state after phase one, before this writer can scan readers or clean up.
            if os.fspath(filename) == state and Path(f"{path}.write").exists():
                Path(state).write_text(f"424242\n{socket.gethostname()}-other\n", encoding="utf-8")

        mocker.patch("os.unlink", autospec=True, side_effect=unlink)
        with pytest.raises(Timeout) as caught:
            writer.acquire_write(timeout=0.02, blocking=blocking)
        assert caught.value.lock_file == str(path)
        assert Path(f"{path}.write").exists()


@pytest.fixture
def abandoned_state(tmp_path: Path) -> Path:
    path: Final = tmp_path / "test.lock"
    Path(f"{path}.state").write_text(f"424242\n{socket.gethostname()}-other\n", encoding="utf-8")
    return path
