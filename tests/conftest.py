from __future__ import annotations

import sys
import threading
from types import TracebackType
from typing import Callable, Tuple, Type, Union

import pytest

_ExcInfoType = Union[Tuple[Type[BaseException], BaseException, TracebackType], Tuple[None, None, None]]


class ExThread(threading.Thread):
    def __init__(self, target: Callable[[], None], name: str) -> None:
        super().__init__(target=target, name=name)
        self.ex: _ExcInfoType | None = None

    def run(self) -> None:
        try:
            super().run()
        except Exception:  # noqa: BLE001 # pragma: no cover
            self.ex = sys.exc_info()  # pragma: no cover

    def join(self, timeout: float | None = None) -> None:
        super().join(timeout=timeout)
        if self.ex is not None:
            raise RuntimeError from self.ex[1]  # pragma: no cover


@pytest.fixture
def ex_thread_cls() -> type[ExThread]:
    return ExThread
