from __future__ import annotations

import sys
import threading
from types import TracebackType
from typing import TYPE_CHECKING, Final

from filelock._core import override

if TYPE_CHECKING:
    from collections.abc import Callable


_ExcInfoType = tuple[type[BaseException], BaseException, TracebackType] | tuple[None, None, None]


class WorkerThread(threading.Thread):
    def __init__(self, target: Callable[[], None], name: str) -> None:
        super().__init__(target=target, name=name, daemon=True)
        self.ex: _ExcInfoType | None = None

    @override
    def run(self) -> None:
        try:
            super().run()
        except Exception:  # pragma: no cover  # a worker that raises fails the test; a healthy run never lands here
            self.ex = sys.exc_info()

    @override
    def join(self, timeout: float | None = None) -> None:
        super().join(timeout=timeout)
        if self.ex is not None:
            raise RuntimeError from self.ex[1]  # pragma: no cover  # only a worker that raised gets re-raised here


# A hung worker would otherwise trigger pytest-timeout and kill the entire Windows session.
THREAD_WAIT: Final[float] = 10


__all__ = [
    "THREAD_WAIT",
    "WorkerThread",
]
