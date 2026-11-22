from __future__ import annotations

import importlib
import os
import subprocess  # ruff:ignore[suspicious-subprocess-import]  # an exited child that a Popen still references
import sys
from errno import EACCES, ENODEV, ENOENT, EPERM
from pathlib import Path
from typing import TYPE_CHECKING, Final

import pytest

import filelock._identity._process  # simplint: ignore[private-import-path]  # tests internal behavior
from filelock._identity import host_name, owner_is_stale, process_alive, process_start_token
from tests.pid_namespace_helpers import (
    INITIAL_PID_NAMESPACE,
    INITIAL_TIME_NAMESPACE,
    pin_pid_namespace,
    pin_proc_pid_namespace,
    pin_time_namespace,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from pytest_mock import MockerFixture

_DEAD_PID: Final[int] = 2**22 + 1
_POSIX_ONLY: Final[pytest.MarkDecorator] = pytest.mark.skipif(sys.platform == "win32", reason="posix kill semantics")
#: NetBSD and other POSIX platforms without a proven start-time source expose no token, so an owner carries none there.
_NEEDS_START_TOKEN: Final[pytest.MarkDecorator] = pytest.mark.skipif(
    process_start_token(os.getpid()) is None, reason="this platform exposes no process start-time source"
)
_LINUX_ONLY: Final[pytest.MarkDecorator] = pytest.mark.skipif(
    sys.platform not in {"linux", "android"}, reason="PID namespaces exist only on Linux"
)
_ANOTHER_TIME_NAMESPACE: Final[int] = 0xF0000A1C
_BOOTTIME_SHIFTED: Final[str] = "monotonic           0         0\nboottime         1000         0\n"
_BOOTTIME_UNSHIFTED: Final[str] = "monotonic           0         0\nboottime            0         0\n"


def test_process_alive_true_for_self() -> None:
    assert process_alive(os.getpid()) is True


def test_process_alive_false_for_dead() -> None:
    assert process_alive(_DEAD_PID) is False


def test_process_alive_false_for_exited_child_still_referenced() -> None:
    # After wait() the Popen still holds the child's handle, which on Windows keeps the exited process openable.
    with subprocess.Popen([sys.executable, "-c", ""]) as child:
        child.wait()
        assert process_alive(child.pid) is False


@_POSIX_ONLY
def test_process_alive_reads_permission_denied_as_alive(mocker: MockerFixture) -> None:  # pragma: win32 no cover
    mocker.patch(
        "filelock._identity._process.os.kill",
        side_effect=OSError(EPERM, "operation not permitted"),
        autospec=True,
    )
    assert process_alive(_DEAD_PID) is True


@_POSIX_ONLY
def test_process_alive_reraises_unexpected_errno(mocker: MockerFixture) -> None:  # pragma: win32 no cover
    mocker.patch("filelock._identity._process.os.kill", side_effect=OSError(ENODEV, "no such device"), autospec=True)
    with pytest.raises(OSError, match="no such device"):  # pragma: win32 no cover
        process_alive(_DEAD_PID)


@_NEEDS_START_TOKEN
def test_process_start_token_is_int_for_self() -> None:
    assert isinstance(process_start_token(os.getpid()), int)


def test_process_start_token_none_for_dead() -> None:
    assert process_start_token(_DEAD_PID) is None


@_LINUX_ONLY  # pragma: linux cover
@_NEEDS_START_TOKEN
@pytest.mark.parametrize(
    "stat",
    [
        pytest.param(b"1 (short) S", id="truncated"),
        pytest.param(b"1 (bad) " + b"0 " * 19 + b"not-an-integer", id="invalid-starttime"),
    ],
)
def test_process_start_token_rejects_malformed_stat(stat: bytes, mocker: MockerFixture) -> None:
    path = Path(f"/proc/{os.getpid()}/stat")
    read_bytes = Path.read_bytes
    mocker.patch.object(
        Path,
        "read_bytes",
        autospec=True,
        side_effect=lambda current: stat if current == path else read_bytes(current),
    )
    assert process_start_token(os.getpid()) is None


@_LINUX_ONLY  # pragma: linux cover
@pytest.mark.parametrize(
    ("namespace", "offsets", "for_children", "readable"),
    [
        pytest.param(INITIAL_TIME_NAMESPACE, _BOOTTIME_SHIFTED, None, True, id="initial"),
        pytest.param(_ANOTHER_TIME_NAMESPACE, _BOOTTIME_UNSHIFTED, None, True, id="container-without-offset"),
        pytest.param(_ANOTHER_TIME_NAMESPACE, "1 0 0\n7 0 0\n", None, True, id="clock-ids-without-offset"),
        pytest.param(_ANOTHER_TIME_NAMESPACE, _BOOTTIME_SHIFTED, None, False, id="boottime-offset"),
        pytest.param(_ANOTHER_TIME_NAMESPACE, "1 0 0\n7 1000 0\n", None, False, id="clock-ids-with-offset"),
        pytest.param(_ANOTHER_TIME_NAMESPACE, _BOOTTIME_UNSHIFTED, 0xF0000A1D, False, id="unshared-not-entered"),
        pytest.param(_ANOTHER_TIME_NAMESPACE, PermissionError(EACCES, "denied"), None, False, id="offsets-unreadable"),
        pytest.param(FileNotFoundError(ENOENT, "missing"), _BOOTTIME_SHIFTED, None, True, id="kernel-before-5.6"),
        pytest.param(PermissionError(EACCES, "denied"), _BOOTTIME_UNSHIFTED, None, False, id="namespace-unreadable"),
    ],
)
def test_process_start_token_read_only_without_a_boottime_offset(
    namespace: int | OSError, offsets: str | OSError, for_children: int | None, readable: bool, mocker: MockerFixture
) -> None:
    pin_time_namespace(mocker, namespace, offsets, for_children=for_children)
    assert (process_start_token(os.getpid()) is not None) is readable


@_LINUX_ONLY  # pragma: linux cover
@pytest.mark.parametrize(
    ("offsets", "readable"),
    [
        pytest.param(_BOOTTIME_UNSHIFTED, True, id="unshifted"),
        pytest.param(_BOOTTIME_SHIFTED, False, id="shifted"),
        pytest.param(FileNotFoundError(ENOENT, "offsets unreadable"), False, id="unreadable"),
    ],
)
def test_process_start_token_after_main_thread_exits(
    offsets: str | OSError, readable: bool, mocker: MockerFixture
) -> None:
    pin_time_namespace(
        mocker,
        _ANOTHER_TIME_NAMESPACE,
        offsets,
        leader_exited=True,
    )
    assert (process_start_token(os.getpid()) is not None) is readable


@_LINUX_ONLY  # pragma: linux cover
@pytest.mark.parametrize(
    ("create", "cached"),
    [pytest.param(Path.touch, True, id="pin-opened"), pytest.param(Path.mkdir, False, id="pin-open-failed")],
)
def test_zero_offset_cache_requires_the_same_pinned_namespace(
    create: Callable[[Path], None], *, cached: bool, tmp_path: Path, mocker: MockerFixture
) -> None:
    namespace = tmp_path / "time"
    create(namespace)
    offsets = tmp_path / "offsets"
    offsets.write_text(_BOOTTIME_UNSHIFTED, encoding="ascii")
    mapped = {
        "/proc/thread-self/ns/time": namespace,
        "/proc/thread-self/ns/time_for_children": namespace,
        f"/proc/{Path('/proc/thread-self').readlink().name}/timens_offsets": offsets,
    }
    mocker.patch(
        "filelock._identity._process.Path", autospec=True, side_effect=lambda name: mapped.get(name, Path(name))
    )
    pid = os.getpid()
    assert (token := process_start_token(pid)) is not None
    offsets.unlink()
    assert (process_start_token(pid) == token) is cached
    namespace.rename(tmp_path / "previous")
    namespace.touch()
    assert process_start_token(pid) is None


@_LINUX_ONLY  # pragma: linux cover
@pytest.mark.parametrize(
    ("self_link", "status", "readable"),
    [
        pytest.param("{pid}", "Name:\tpython\nNSpid:\t{pid}\n", True, id="own"),
        pytest.param("{pid}", "Name:\tpython\n", True, id="own-kernel-before-4.1"),
        pytest.param("4711", "Name:\tpython\nNSpid:\t4711\t{pid}\n", False, id="outer"),
        pytest.param("{pid}", "Name:\tpython\nNSpid:\t{pid}\t{pid}\n", False, id="outer-with-the-same-number"),
        pytest.param("4711", "Name:\tpython\n", False, id="outer-kernel-before-4.1"),
        pytest.param(FileNotFoundError(ENOENT, "missing"), "Name:\tpython\n", False, id="without-this-process"),
        pytest.param("{pid}", PermissionError(EACCES, "denied"), False, id="status-unreadable"),
    ],
)
def test_process_start_token_read_only_from_a_proc_of_this_pid_namespace(
    self_link: str | OSError, status: str | OSError, readable: bool, mocker: MockerFixture
) -> None:
    pin_proc_pid_namespace(mocker, self_link, status)
    assert (process_start_token(os.getpid()) is not None) is readable


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS sysctl probe")
def test_darwin_sysctl_probe_failure_reads_no_token(mocker: MockerFixture) -> None:  # pragma: darwin cover
    mocker.patch("filelock._identity._process._LIBC.sysctl", return_value=-1, autospec=True)
    assert process_start_token(os.getpid()) is None


def test_owner_is_stale_foreign_host_never_reclaimed() -> None:
    assert owner_is_stale(os.getpid(), "another-host.example.com", 1) is False


def test_owner_is_stale_dead_process_reclaimed() -> None:
    assert owner_is_stale(_DEAD_PID, host_name(), 1) is True


def test_owner_is_stale_live_process_without_token_held() -> None:
    assert owner_is_stale(os.getpid(), host_name(), None) is False


def test_owner_is_stale_live_process_matching_token_held() -> None:
    assert owner_is_stale(os.getpid(), host_name(), process_start_token(os.getpid())) is False


@_NEEDS_START_TOKEN
def test_owner_is_stale_live_process_mismatched_token_reclaimed() -> None:
    token: Final[int | None] = process_start_token(os.getpid())
    assert token is not None
    # Far enough ahead that no task on this kernel started then.
    assert owner_is_stale(os.getpid(), host_name(), token + 10**9) is True


@_LINUX_ONLY
def test_owner_is_stale_live_process_across_time_namespaces_held(mocker: MockerFixture) -> None:  # pragma: linux cover
    # The kernel shifts starttime by the reader's boottime offset, so a token recorded outside never matches inside.
    token: Final[int | None] = process_start_token(os.getpid())
    assert token is not None
    pin_time_namespace(mocker, _ANOTHER_TIME_NAMESPACE, _BOOTTIME_SHIFTED)
    assert owner_is_stale(os.getpid(), host_name(), token + 100_000) is False


@_LINUX_ONLY
def test_owner_is_stale_live_process_behind_an_outer_proc_held(mocker: MockerFixture) -> None:  # pragma: linux cover
    # unshare --pid without --mount-proc keeps the outer procfs, where this PID is whichever task has the number there.
    token: Final[int | None] = process_start_token(os.getpid())
    assert token is not None
    pin_pid_namespace(mocker, 0xF0000001)
    pin_proc_pid_namespace(mocker, "4711", f"Name:\tpython\nNSpid:\t4711\t{os.getpid()}\n")
    assert owner_is_stale(os.getpid(), host_name(), token + 10**9) is False


@_LINUX_ONLY  # pragma: linux cover
def test_owner_is_stale_live_process_boot_id_hidden_from_holder_held() -> None:
    token: Final[int | None] = process_start_token(os.getpid())
    assert token is not None
    assert owner_is_stale(os.getpid(), host_name(), token % 2**64) is False


@_LINUX_ONLY  # pragma: linux cover
def test_owner_is_stale_live_process_boot_id_hidden_from_contender_held(hide_boot_id: Callable[[], None]) -> None:
    token: Final[int | None] = process_start_token(os.getpid())
    assert token is not None
    hide_boot_id()
    assert owner_is_stale(os.getpid(), host_name(), token) is False


@_LINUX_ONLY  # pragma: linux cover
@pytest.mark.parametrize("same_device", [pytest.param(True, id="same-procfs"), pytest.param(False, id="other-device")])
def test_owner_is_stale_live_process_with_boot_id_mounted_over_for_contender_held(
    mount_over_boot_id: Callable[[bool], None], *, same_device: bool
) -> None:
    token: Final[int | None] = process_start_token(os.getpid())
    assert token is not None
    mount_over_boot_id(same_device)
    assert owner_is_stale(os.getpid(), host_name(), token) is False


@_LINUX_ONLY  # pragma: linux cover
@pytest.mark.parametrize("same_device", [pytest.param(True, id="same-procfs"), pytest.param(False, id="other-device")])
def test_owner_is_stale_live_process_with_boot_id_mounted_over_for_holder_held(
    mount_over_boot_id: Callable[[bool], None], *, same_device: bool
) -> None:
    mount_over_boot_id(same_device)
    token: Final[int | None] = process_start_token(os.getpid())
    assert token is not None
    importlib.reload(filelock._identity._process)
    assert owner_is_stale(os.getpid(), host_name(), token) is False


if sys.platform in {"linux", "android"}:  # pragma: linux cover

    def test_boot_id_with_non_ascii_mountpoint(tmp_path: Path, mocker: MockerFixture) -> None:
        boot_id = tmp_path / "proc/sys/kernel/random/boot_id"
        boot_id.parent.mkdir(parents=True)
        boot_id.write_text("00000000-0000-0000-0000-00000000002a", encoding="ascii")
        mounts = tmp_path / "proc/self/mountinfo"
        mounts.parent.mkdir()
        mounts.write_text("1 0 0:10 / /mnt/café rw - tmpfs tmpfs rw\n", encoding="utf-8")
        mocker.patch.object(
            filelock._identity._process,
            "Path",
            autospec=True,
            side_effect=lambda path: tmp_path / path.lstrip("/"),
        )
        assert filelock._identity._process._read_boot_id() == 42


@_LINUX_ONLY  # pragma: linux cover
def test_unreadable_mountinfo_keeps_only_starttime(mocker: MockerFixture) -> None:
    token: Final[int | None] = process_start_token(os.getpid())
    assert token is not None
    blocked = mocker.patch.object(Path, "open", autospec=True, side_effect=PermissionError(EACCES, "denied"))
    importlib.reload(filelock._identity._process)
    mocker.stop(blocked)
    try:
        assert process_start_token(os.getpid()) == token % 2**64
    finally:
        importlib.reload(filelock._identity._process)


@_LINUX_ONLY  # pragma: linux cover
def test_owner_is_stale_recycled_pid_boot_id_hidden_from_holder_reclaimed() -> None:
    token: Final[int | None] = process_start_token(os.getpid())
    assert token is not None
    assert owner_is_stale(os.getpid(), host_name(), token % 2**64 + 10**9) is True


@_LINUX_ONLY  # pragma: linux cover
def test_owner_is_stale_recycled_pid_boot_id_hidden_from_contender_reclaimed(
    hide_boot_id: Callable[[], None],
) -> None:
    token: Final[int | None] = process_start_token(os.getpid())
    assert token is not None
    hide_boot_id()
    assert owner_is_stale(os.getpid(), host_name(), token % 2**64 + 10**9) is True


@pytest.fixture
def owner_in_another_namespace(mocker: MockerFixture) -> None:  # pragma: linux cover
    # Older releases wrote the container PID even when the observer reads an outer procfs.
    pin_pid_namespace(mocker, INITIAL_PID_NAMESPACE)
    own_status: Final[Path] = Path(f"/proc/{os.getpid()}/status")
    read_bytes = Path.read_bytes
    mocker.patch.object(
        Path,
        "read_bytes",
        autospec=True,
        side_effect=lambda path: (
            f"NSpid:\t{os.getpid()}\t{_DEAD_PID}\n".encode() if path == own_status else read_bytes(path)
        ),
    )


@_LINUX_ONLY  # pragma: linux cover
@pytest.mark.usefixtures("owner_in_another_namespace")
@pytest.mark.parametrize(
    "recorded",
    [
        pytest.param(lambda token: token, id="start-token-matches"),
        pytest.param(lambda token: token % 2**64, id="holder-without-boot-id"),
    ],
)
def test_owner_is_stale_owner_running_in_another_namespace_held(recorded: Callable[[int], int]) -> None:
    token: Final[int | None] = process_start_token(os.getpid())
    assert token is not None
    assert owner_is_stale(_DEAD_PID, host_name(), recorded(token)) is False


@_LINUX_ONLY  # pragma: linux cover
@pytest.mark.usefixtures("owner_in_another_namespace")
def test_owner_is_stale_owner_running_in_another_namespace_contender_without_boot_id_held(
    hide_boot_id: Callable[[], None],
) -> None:
    token: Final[int | None] = process_start_token(os.getpid())
    assert token is not None
    hide_boot_id()
    assert owner_is_stale(_DEAD_PID, host_name(), token) is False


@_LINUX_ONLY  # pragma: linux cover
@pytest.mark.usefixtures("owner_in_another_namespace")
def test_owner_is_stale_owner_running_in_another_namespace_contender_with_boottime_offset_held(
    mocker: MockerFixture,
) -> None:
    token: Final[int | None] = process_start_token(os.getpid())
    assert token is not None
    pin_time_namespace(mocker, _ANOTHER_TIME_NAMESPACE, _BOOTTIME_SHIFTED)
    assert owner_is_stale(_DEAD_PID, host_name(), token) is False


@_LINUX_ONLY  # pragma: linux cover
@pytest.mark.parametrize("forged", [pytest.param("x", id="text"), pytest.param(str(_DEAD_PID), id="dead-pid")])
@pytest.mark.parametrize(
    "separator",
    [
        pytest.param("\r", id="CR"),
        pytest.param("\v", id="VT"),
        pytest.param("\f", id="FF"),
        pytest.param("\x1c", id="FS"),
        pytest.param("\x1d", id="GS"),
        pytest.param("\x1e", id="RS"),
    ],
)
def test_owner_is_stale_task_name_shaped_like_a_namespace_line_reclaimed(
    separator: str, forged: str, mocker: MockerFixture
) -> None:
    # A task picks its own name and /proc/<pid>/status prints it raw but for "\n" and "\\".
    pin_pid_namespace(mocker, INITIAL_PID_NAMESPACE)
    comm: Final[Path] = Path("/proc/self/comm")
    name: Final[bytes] = comm.read_bytes().rstrip(b"\n")
    comm.write_bytes(f"{separator}NSpid:\t{forged}".encode())
    try:
        assert owner_is_stale(_DEAD_PID, host_name(), process_start_token(os.getpid())) is True
    finally:
        comm.write_bytes(name)


@_LINUX_ONLY  # pragma: linux cover
@pytest.mark.parametrize(
    ("boot_flip", "scans"),
    [
        # Workers forked together can share a start token.
        pytest.param(0, 1, id="same-tick"),
        pytest.param(1 << 64, 0, id="another-boot"),
    ],
)
def test_owner_is_stale_dead_owner_by_start_token_reclaimed(boot_flip: int, scans: int, mocker: MockerFixture) -> None:
    pin_pid_namespace(mocker, INITIAL_PID_NAMESPACE)
    scandir = mocker.patch("filelock._identity._process.os.scandir", autospec=True, side_effect=os.scandir)
    token: Final[int | None] = process_start_token(os.getpid())
    assert token is not None
    assert (owner_is_stale(_DEAD_PID, host_name(), token ^ boot_flip), scandir.call_count) == (True, scans)


@_LINUX_ONLY
def test_owner_is_stale_without_a_readable_proc_reclaimed(mocker: MockerFixture) -> None:  # pragma: linux cover
    pin_pid_namespace(mocker, INITIAL_PID_NAMESPACE)
    mocker.patch(
        "filelock._identity._process.os.scandir", autospec=True, side_effect=PermissionError(EACCES, "hidepid")
    )
    assert owner_is_stale(_DEAD_PID, host_name(), process_start_token(os.getpid())) is True


@_LINUX_ONLY  # pragma: linux cover
@_NEEDS_START_TOKEN
def test_owner_is_stale_sibling_pid_namespace_never_reclaimed(mocker: MockerFixture) -> None:
    # Containers in one pod can share a hostname and assign the same PID to different processes.
    token: Final[int | None] = process_start_token(os.getpid())
    assert token is not None
    pin_pid_namespace(mocker, 0xF0000001)
    holder_host: Final[str] = host_name()
    pin_pid_namespace(mocker, 0xF0000002)
    assert owner_is_stale(os.getpid(), holder_host, token + 1) is False


def test_owner_is_stale_live_process_unreadable_token_held(mocker: MockerFixture) -> None:
    # A live PID whose current start token cannot be read is indistinguishable from the recorded owner, so it holds.
    mocker.patch("filelock._identity._process.process_start_token", return_value=None, autospec=True)
    assert owner_is_stale(os.getpid(), host_name(), 987654) is False
