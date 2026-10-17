from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from pytest_mock import MockerFixture

#: The inode the kernel ABI fixes for the initial PID namespace.
INITIAL_PID_NAMESPACE: Final[int] = 0xEFFFFFFC
#: The inode the kernel ABI fixes for the initial time namespace.
INITIAL_TIME_NAMESPACE: Final[int] = 0xEFFFFFFA


def pin_pid_namespace(mocker: MockerFixture, namespace: int | OSError) -> None:
    """Answer a stat of ``/proc/self/ns/pid`` with *namespace*, as its inode or as the error the stat raises."""
    _pin_namespace(mocker, Path("/proc/self/ns/pid"), namespace)


def pin_time_namespace(  # pragma: linux cover
    mocker: MockerFixture, namespace: int | OSError, offsets: str | OSError, *, for_children: int | None = None
) -> None:
    """
    Answer for this process's time namespace.

    :param namespace: the inode a stat of ``/proc/self/ns/time`` reports, or the error it raises
    :param offsets: what reading ``/proc/self/timens_offsets`` returns, or the error it raises
    :param for_children: the inode of ``/proc/self/ns/time_for_children``, when an unshare made it differ
    """
    _pin_namespace(mocker, Path("/proc/self/ns/time"), namespace)
    _pin_namespace(mocker, Path("/proc/self/ns/time_for_children"), namespace if for_children is None else for_children)
    answer: Final = (
        mocker.Mock(side_effect=offsets) if isinstance(offsets, OSError) else mocker.Mock(return_value=offsets)
    )
    read_text: Final = Path.read_text
    mocker.patch.object(
        Path,
        "read_text",
        new=lambda path, *args, **kwargs: (
            answer() if path == Path("/proc/self/timens_offsets") else read_text(path, *args, **kwargs)
        ),
    )


def _pin_namespace(mocker: MockerFixture, link: Path, namespace: int | OSError) -> None:
    answer: Final = (
        mocker.Mock(side_effect=namespace)
        if isinstance(namespace, OSError)
        else mocker.Mock(return_value=os.stat_result((0, namespace, 0, 0, 0, 0, 0, 0, 0, 0)))
    )
    # A plain function, not an autospec: a test that moves to another namespace pins again over this patch.
    stat: Final = Path.stat
    mocker.patch.object(Path, "stat", new=lambda path, **kwargs: answer() if path == link else stat(path, **kwargs))
