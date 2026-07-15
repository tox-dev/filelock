from __future__ import annotations

import os
import subprocess  # noqa: S404  # the process must crash between two filesystem operations
import sys
import textwrap
from typing import TYPE_CHECKING, Final

import pytest

from filelock import StrictSoftFileLock, Timeout

if TYPE_CHECKING:
    from pathlib import Path

# The crash points are hit through sys.addaudithook on os.link and os.remove events, which only CPython emits. PyPy
# runs the audit hook but never fires those events, so the child would finish the acquisition instead of crashing.
pytestmark = pytest.mark.skipif(
    sys.implementation.name != "cpython",
    reason="crash injection needs the os.link and os.remove audit events CPython emits",
)

_CRASH_STATUS: Final[int] = 73


def test_strict_soft_recovers_every_claim_for_crashed_token(tmp_path: Path) -> None:
    lock_path = tmp_path / "resource.lock"
    result = subprocess.run(
        [sys.executable, "-c", _crash_after_held_publication(), os.fspath(lock_path)],
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

    crashed_token = claims[0].token
    for claim in lock.claims:
        if claim.token == crashed_token:
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
    lock = StrictSoftFileLock(lock_path, timeout=0)
    assert (lock.claims, tuple(tmp_path.glob("**/.*.private-v1-*.tmp"))) == ((), ())
    with lock:
        assert lock.is_locked


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
        1,
        1,
    )
    assert tuple(tmp_path.glob("**/.*.private-v1-*.tmp")) == ()
    lock.force_break(claims[0].name)
    with lock:
        assert lock.is_locked


def _crash_after_held_publication() -> str:
    return textwrap.dedent(
        f"""
        from __future__ import annotations

        import os
        import sys

        from filelock import StrictSoftFileLock

        def exit_before_intent_cleanup(event: str, args: tuple[str | int, ...]) -> None:
            if event == "os.remove" and os.path.basename(os.fsdecode(args[0])).startswith("intent-"):
                os._exit({_CRASH_STATUS})

        sys.addaudithook(exit_before_intent_cleanup)
        StrictSoftFileLock(sys.argv[1]).acquire()
        """
    )


def _crash_during_private_publication(stage: str) -> str:
    return textwrap.dedent(
        f"""
        from __future__ import annotations

        import os
        import sys

        from filelock import StrictSoftFileLock

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
