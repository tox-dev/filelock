from __future__ import annotations

import os
import stat
import sys
from errno import EIO
from functools import partial
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest

import filelock._filesystem._ownership  # simplint: ignore[private-import-path]  # bypass the UID probe cache
from filelock._filesystem import foreign_owner

if TYPE_CHECKING:
    import ctypes
    from collections.abc import Callable, Generator

    from pytest_mock import MockerFixture


@pytest.mark.skipif(sys.platform not in {"linux", "android"}, reason="overflowuid is a Linux kernel setting")
@pytest.mark.parametrize(
    ("overflowuid", "expected"),
    [
        pytest.param("65534", 65534, id="ambiguous"),
        pytest.param("65535", None, id="distinct"),
        pytest.param("invalid", 65534, id="invalid"),
    ],
)
def test_foreign_owner_refuses_ambiguous_uid(  # pragma: win32 no cover  # pragma: darwin no cover
    mocker: MockerFixture, overflowuid: str, expected: int | None
) -> None:
    mocker.patch("filelock._filesystem._ownership.os.geteuid", autospec=True, return_value=65534)
    mocker.patch.object(Path, "read_text", autospec=True, return_value=overflowuid)
    owner = os.stat_result((stat.S_IFDIR | 0o755, 0, 0, 0, 65534, 0, 0, 0, 0, 0))
    parent = os.stat_result((stat.S_IFDIR | 0o1777, 0, 0, 0, 0, 0, 0, 0, 0, 0))

    assert foreign_owner(owner, parent) == expected


@pytest.mark.skipif(sys.platform not in {"linux", "android"}, reason="overflowuid is a Linux kernel setting")
def test_foreign_owner_refuses_uid_when_overflowuid_is_unreadable(  # pragma: win32 no cover  # pragma: darwin no cover
    mocker: MockerFixture,
) -> None:
    mocker.patch("filelock._filesystem._ownership.os.geteuid", autospec=True, return_value=65534)
    mocker.patch.object(Path, "read_text", autospec=True, side_effect=OSError("unreadable"))
    owner = os.stat_result((stat.S_IFDIR | 0o755, 0, 0, 0, 65534, 0, 0, 0, 0, 0))
    parent = os.stat_result((stat.S_IFDIR | 0o1777, 0, 0, 0, 0, 0, 0, 0, 0, 0))

    assert foreign_owner(owner, parent) == 65534


@pytest.mark.skipif(sys.platform not in {"linux", "android"}, reason="mount IDs and UID maps are Linux procfs files")
@pytest.mark.parametrize(
    ("uid_map", "fdinfo", "mountinfo", "allow"),
    [
        pytest.param(b"0 0 4294967295", b"mnt_id:\t18", b"18 4 0:3 / /tmp rw - tmpfs tmpfs rw", True, id="mapped"),
        pytest.param(
            b"0 0 4294967295", b"mnt_id:\t18", b"18 4 0:3 / /tmp rw - overlay overlay rw", False, id="overlay"
        ),
        pytest.param(b"0 0 65535", b"mnt_id:\t18", b"18 4 0:3 / /tmp rw - tmpfs tmpfs rw", False, id="partial"),
        pytest.param(
            b"0 0 4294967295", b"mnt_id:\t18", b"18 4 0:3 / /tmp rw,idmapped - tmpfs tmpfs rw", False, id="idmapped"
        ),
        pytest.param(b"0 0 4294967295", b"mnt_id:\t18", b"19 4 0:3 / /tmp rw - tmpfs tmpfs rw", False, id="detached"),
        pytest.param(b"0 0 4294967295", b"pos:\t0", b"", False, id="no-mount-id"),
    ],
)
def test_open_file_with_unknown_overflow_uid(  # pragma: win32 no cover  # pragma: darwin no cover
    tmp_path: Path, mocker: MockerFixture, uid_map: bytes, fdinfo: bytes, mountinfo: bytes, allow: bool
) -> None:
    mocker.patch("filelock._filesystem._ownership._linux_owner_probe", autospec=True, return_value=None)
    proc = tmp_path / "thread-self"
    (proc / "fdinfo").mkdir(parents=True)
    (proc / "uid_map").write_bytes(uid_map)
    (proc / "mountinfo").write_bytes(mountinfo)
    paths = {"/proc/thread-self": proc, "/proc/sys/kernel/overflowuid": tmp_path / "missing"}
    mocker.patch("filelock._filesystem._ownership.Path", autospec=True, side_effect=paths.__getitem__)
    tmp_path.chmod(0o1700)
    with (tmp_path / "lock").open("xb") as marker:
        fd = marker.fileno()
        (proc / "fdinfo" / str(fd)).write_bytes(fdinfo)
        owner = os.fstat(fd)
        assert (foreign_owner(owner, tmp_path.stat(), fd=fd) is None) is allow


@pytest.fixture
def sticky_fd(tmp_path: Path) -> Generator[int]:  # pragma: linux cover
    tmp_path.chmod(0o1700)
    with (tmp_path / "marker").open("xb") as marker:
        yield marker.fileno()


@pytest.fixture
def reported_owner(  # pragma: linux cover
    sticky_fd: int, tmp_path: Path, mocker: MockerFixture
) -> Callable[[], int | None]:
    # Each test changes the native bindings, which production looks up once.
    mocker.patch.object(
        filelock._filesystem._ownership,
        "_linux_owner_probe",
        new=filelock._filesystem._ownership._linux_owner_probe.__wrapped__,
    )
    mocker.patch.object(Path, "read_text", autospec=True, side_effect=FileNotFoundError)
    mocker.patch.object(Path, "read_bytes", autospec=True, side_effect=FileNotFoundError)
    return partial(foreign_owner, os.fstat(sticky_fd), tmp_path.stat(), fd=sticky_fd)


@pytest.fixture
def local_filesystem(mocker: MockerFixture, fs_type: int | bytes, capability: int) -> None:  # pragma: linux cover
    ctypes_mod = pytest.importorskip("ctypes")
    libc = ctypes_mod.CDLL(None)
    if isinstance(fs_type, bytes):
        mocker.patch.object(sys, "byteorder", new="big")
        mocker.patch("platform.machine", autospec=True, return_value="s390x")
        header = fs_type
    else:
        mocker.patch.object(sys, "byteorder", new="little")
        header = fs_type.to_bytes(8, "little")

    def statfs(_fd: int, info: ctypes.Array[ctypes.c_long]) -> int:
        ctypes_mod.memmove(info, header, len(header))
        return 0

    def capget(_version: ctypes.Array[ctypes.c_uint32], caps: ctypes.Array[ctypes.c_uint32]) -> int:
        caps[0] = capability
        return 0

    mocker.patch.object(libc, "fstatfs", new=mocker.create_autospec(statfs, side_effect=statfs))
    mocker.patch.object(libc, "capget", new=mocker.create_autospec(capget, side_effect=capget))
    mocker.patch.object(ctypes_mod, "CDLL", autospec=True, return_value=libc)


@pytest.mark.skipif(sys.platform != "linux", reason="O_NOATIME is a Linux ownership check")
@pytest.mark.parametrize(
    ("fs_type", "capability", "allowed"),
    [
        pytest.param(0x01021994, 0, True, id="local"),
        pytest.param(0x794C7630, 0, True, id="overlay"),
        pytest.param(bytes.fromhex("794c7630 00001000"), 0, True, id="s390x-overlay"),
        pytest.param(bytes.fromhex("65735546 01021994"), 0, False, id="s390x-unsupported"),
        pytest.param(0x65735546, 0, False, id="fuse"),
        pytest.param(0x6969, 0, False, id="nfs"),
        pytest.param(0x01021994, 1 << 3, False, id="privileged"),
    ],
)
@pytest.mark.usefixtures("local_filesystem")
def test_descriptor_ownership_preserves_flags(  # pragma: linux cover
    sticky_fd: int, reported_owner: Callable[[], int | None], allowed: bool
) -> None:
    fcntl = pytest.importorskip("ctypes").CDLL(None).fcntl
    before = fcntl(sticky_fd, 3)
    assert (reported_owner() is None, fcntl(sticky_fd, 3)) == (allowed, before)


@pytest.mark.skipif(sys.platform != "linux", reason="O_NOATIME is a Linux ownership check")
@pytest.mark.parametrize(("fs_type", "capability"), [(0x01021994, 0)])
@pytest.mark.parametrize(
    "responses",
    [
        pytest.param([getattr(os, "O_NOATIME", 0)], id="already-set"),
        pytest.param([0, -1], id="permission-denied"),
    ],
)
@pytest.mark.usefixtures("local_filesystem")
def test_descriptor_ownership_needs_a_new_permission_check(  # pragma: linux cover
    mocker: MockerFixture, sticky_fd: int, reported_owner: Callable[[], int | None], responses: list[int]
) -> None:
    mocker.patch.object(pytest.importorskip("ctypes").CDLL(None), "fcntl", autospec=True, side_effect=responses)
    assert reported_owner() == os.fstat(sticky_fd).st_uid


@pytest.mark.skipif(sys.platform != "linux", reason="O_NOATIME is a Linux ownership check")
@pytest.mark.parametrize(("fs_type", "capability"), [(0x01021994, 0)])
@pytest.mark.usefixtures("local_filesystem")
def test_descriptor_ownership_propagates_a_flag_restore_error(  # pragma: linux cover
    mocker: MockerFixture, reported_owner: Callable[[], int | None]
) -> None:
    ctypes_mod = pytest.importorskip("ctypes")
    mocker.patch.object(ctypes_mod.CDLL(None), "fcntl", autospec=True, side_effect=[0, 0, -1])
    mocker.patch.object(ctypes_mod, "get_errno", autospec=True, return_value=EIO)
    with pytest.raises(OSError, match=os.strerror(EIO)):
        reported_owner()


@pytest.fixture(params=["ctypes", "dynamic-loader", "O_NOATIME", "capget"])
def unavailable_owner_check(request: pytest.FixtureRequest, mocker: MockerFixture) -> None:  # pragma: linux cover
    match request.param:
        case "ctypes":
            mocker.patch.dict(sys.modules, {"ctypes": None})
        case "dynamic-loader":
            mocker.patch("ctypes.CDLL", autospec=True, side_effect=OSError("dynamic loading unavailable"))
        case "O_NOATIME":
            mocker.patch.object(os, "O_NOATIME", new=0)
        case _:
            mocker.patch("ctypes.CDLL", autospec=True, return_value=SimpleNamespace(fstatfs=None, fcntl=None))


@pytest.mark.skipif(sys.platform != "linux", reason="O_NOATIME is a Linux ownership check")
@pytest.mark.usefixtures("unavailable_owner_check")
def test_ownership_without_native_bindings(  # pragma: linux cover
    sticky_fd: int, reported_owner: Callable[[], int | None]
) -> None:
    assert reported_owner() == os.fstat(sticky_fd).st_uid
