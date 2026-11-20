from __future__ import annotations

import os
from errno import ENOENT
from pathlib import Path
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from pytest_mock import MockerFixture

#: The inode the kernel ABI fixes for the initial PID namespace.
INITIAL_PID_NAMESPACE: Final[int] = 0xEFFFFFFC
#: The inode the kernel ABI fixes for the initial time namespace.
INITIAL_TIME_NAMESPACE: Final[int] = 0xEFFFFFFA


def pin_pid_namespace(mocker: MockerFixture, namespace: int | OSError) -> None:
    # Answer a stat of ``/proc/self/ns/pid`` with *namespace*, as its inode or as the error the stat raises.
    _pin_namespace(mocker, Path("/proc/self/ns/pid"), namespace)


def _pin_namespace(mocker: MockerFixture, link: Path, namespace: int | OSError) -> None:
    answer = (
        mocker.create_autospec(os.stat, side_effect=namespace)
        if isinstance(namespace, OSError)
        else mocker.create_autospec(os.stat, return_value=os.stat_result((0, namespace, 0, 0, 0, 0, 0, 0, 0, 0)))
    )
    # A plain function, not an autospec: a test that moves to another namespace pins again over this patch.
    stat = Path.stat
    mocker.patch.object(Path, "stat", new=lambda path, **kwargs: answer(path) if path == link else stat(path, **kwargs))


def pin_proc_pid_namespace(  # pragma: linux cover
    mocker: MockerFixture, self_link: str | OSError, status: str | OSError
) -> None:
    link = (
        mocker.create_autospec(Path.readlink, side_effect=self_link)
        if isinstance(self_link, OSError)
        else mocker.create_autospec(Path.readlink, return_value=Path(self_link.format(pid=os.getpid())))
    )
    answer = (
        mocker.create_autospec(Path.read_bytes, side_effect=status)
        if isinstance(status, OSError)
        else mocker.create_autospec(Path.read_bytes, return_value=status.format(pid=os.getpid()).encode())
    )
    readlink = Path.readlink
    read_bytes = Path.read_bytes
    mocker.patch.object(Path, "readlink", new=lambda path: link(path) if path == Path("/proc/self") else readlink(path))
    mocker.patch.object(
        Path, "read_bytes", new=lambda path: answer(path) if path == Path("/proc/self/status") else read_bytes(path)
    )


def pin_time_namespace(  # pragma: linux cover
    mocker: MockerFixture,
    namespace: int | OSError,
    offsets: str | OSError,
    *,
    for_children: int | None = None,
    leader_exited: bool = False,
) -> None:
    # Answer for this process's time namespace.
    #
    # :param namespace: the inode a stat of ``/proc/thread-self/ns/time`` reports, or the error it raises
    # :param offsets: what reading the worker's ``/proc/<tid>/timens_offsets`` returns, or the error it raises
    # :param for_children: the inode of ``/proc/thread-self/ns/time_for_children``, when an unshare made it differ
    _pin_namespace(mocker, Path("/proc/thread-self/ns/time"), namespace)
    _pin_namespace(
        mocker,
        Path("/proc/thread-self/ns/time_for_children"),
        namespace if for_children is None else for_children,
    )
    if leader_exited:
        _pin_namespace(mocker, Path("/proc/self/ns/time"), FileNotFoundError(ENOENT, "leader exited"))
    readlink = Path.readlink
    mocker.patch.object(
        Path,
        "readlink",
        new=lambda path: Path("4711/task/4712") if path == Path("/proc/thread-self") else readlink(path),
    )
    answer = (
        mocker.create_autospec(Path.read_text, side_effect=offsets)
        if isinstance(offsets, OSError)
        else mocker.create_autospec(Path.read_text, return_value=offsets)
    )
    read_text = Path.read_text
    mocker.patch.object(
        Path,
        "read_text",
        new=lambda path, *args, **kwargs: (
            answer(path, *args, **kwargs)
            if path == Path("/proc/4712/timens_offsets")
            else read_text(path, *args, **kwargs)
        ),
    )
