from __future__ import annotations

import os
import subprocess  # ruff:ignore[suspicious-subprocess-import]  # the process must crash between two filesystem operations
import sys
import textwrap
from typing import TYPE_CHECKING, Final

import pytest

from filelock import StrictSoftFileLock, Timeout

if TYPE_CHECKING:
    from pathlib import Path

# POSIX crash injection uses CPython's os.link and os.remove audit events; Windows hooks handle rename in the child.
pytestmark: Final[list[pytest.MarkDecorator]] = [
    pytest.mark.skipif(
        sys.platform != "win32" and sys.implementation.name != "cpython",
        reason="POSIX crash injection needs the os.link and os.remove audit events CPython emits",
    ),
    pytest.mark.requires_hard_links,
]

_CRASH_STATUS: Final[int] = 73


def test_strict_soft_recovers_every_claim_for_crashed_token(tmp_path: Path) -> None:
    lock_path = tmp_path / "resource.lock"
    # os._exit bypasses finalizers, leaving the crashed holder's intent and held claims on disk.
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            textwrap.dedent(f"""
        from __future__ import annotations

        import os
        import sys

        from filelock import StrictSoftFileLock

        lock = StrictSoftFileLock(sys.argv[1])
        lock.acquire()
        os._exit({_CRASH_STATUS})
        """),
            os.fspath(lock_path),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    lock = StrictSoftFileLock(lock_path, timeout=0)
    claims = lock.claims

    assert (result.returncode, result.stdout, result.stderr, [claim.state for claim in claims]) == (
        _CRASH_STATUS,
        "",
        "",
        ["held", "intent"],
    )
    assert len({claim.token for claim in claims}) == 1
    with pytest.raises(Timeout):
        lock.acquire()

    for claim in lock.claims:
        lock.force_break(claim.name)
    with lock:
        assert lock.is_locked


def test_strict_soft_reclaims_crash_before_private_publication(tmp_path: Path) -> None:
    lock_path = tmp_path / "resource.lock"
    result = subprocess.run(
        [sys.executable, "-c", _crash_during_private_publication("before-link"), os.fspath(lock_path)],
        check=False,
        capture_output=True,
        text=True,
    )
    private_records = tuple(tmp_path.glob("**/.*.private-v1-*.tmp"))

    assert (result.returncode, result.stdout, result.stderr, len(private_records)) == (_CRASH_STATUS, "", "", 1)
    os.utime(private_records[0], (0, 0))
    with StrictSoftFileLock(lock_path, timeout=0):
        assert tuple(tmp_path.glob("**/.*.private-v1-*.tmp")) == (private_records if os.name == "nt" else ())


def _crash_during_private_publication(stage: str) -> str:
    return textwrap.dedent(
        f"""
        from __future__ import annotations

        import os
        import sys

        from filelock import StrictSoftFileLock

        if os.name == "nt":
            from filelock._soft._strict import _windows as strict_windows

            rename_claim = strict_windows.rename_open_file

            def exit_at_claim_rename(fd: int, path: str) -> None:
                intent = os.path.basename(path).startswith("intent-")
                if intent and {stage!r} == "before-link":
                    os._exit({_CRASH_STATUS})
                rename_claim(fd, path)
                if intent and {stage!r} == "after-link":
                    os._exit({_CRASH_STATUS})

            strict_windows.rename_open_file = exit_at_claim_rename

        def exit_at_publication_stage(event: str, args: tuple[str | int, ...]) -> None:
            if event not in {{"os.link", "os.remove"}}:
                return
            path = os.path.basename(os.fsdecode(args[1] if event == "os.link" else args[0]))
            before_link = {stage!r} == "before-link" and event == "os.link" and path.startswith("intent-")
            after_link = (
                {stage!r} == "after-link"
                and event == "os.remove"
                and path.startswith(".intent-")
                and ".private-v1-" in path
                and path.endswith(".tmp")
            )
            if before_link or after_link:
                os._exit({_CRASH_STATUS})

        sys.addaudithook(exit_at_publication_stage)
        StrictSoftFileLock(sys.argv[1]).acquire()
        """
    )


def test_strict_soft_reclaims_crash_after_private_publication(tmp_path: Path) -> None:
    lock_path = tmp_path / "resource.lock"
    result = subprocess.run(
        [sys.executable, "-c", _crash_during_private_publication("after-link"), os.fspath(lock_path)],
        check=False,
        capture_output=True,
        text=True,
    )
    private_records = tuple(tmp_path.glob("**/.*.private-v1-*.tmp"))
    lock = StrictSoftFileLock(lock_path, timeout=0)
    claims = lock.claims

    assert (result.returncode, result.stdout, result.stderr, len(private_records), len(claims)) == (
        _CRASH_STATUS,
        "",
        "",
        0 if sys.platform == "win32" else 1,
        1,
    )
    assert tuple(tmp_path.glob("**/.*.private-v1-*.tmp")) == (private_records if os.name == "nt" else ())
    lock.force_break(claims[0].name)
    with lock:
        assert lock.is_locked
