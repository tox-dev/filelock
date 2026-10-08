from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from pytest_mock import MockerFixture

#: The inode the kernel ABI fixes for the initial PID namespace.
INITIAL_PID_NAMESPACE: Final[int] = 0xEFFFFFFC


def pin_pid_namespace(mocker: MockerFixture, namespace: int | OSError) -> None:
    """Answer a stat of ``/proc/self/ns/pid`` with *namespace*, as its inode or as the error the stat raises."""
    answer: Final = (
        mocker.Mock(side_effect=namespace)
        if isinstance(namespace, OSError)
        else mocker.Mock(return_value=os.stat_result((0, namespace, 0, 0, 0, 0, 0, 0, 0, 0)))
    )
    # A plain function, not an autospec: a test that moves to another namespace pins again over this patch.
    stat: Final = Path.stat
    mocker.patch.object(
        Path, "stat", new=lambda path, **kwargs: answer() if path == Path("/proc/self/ns/pid") else stat(path, **kwargs)
    )
