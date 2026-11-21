from __future__ import annotations

from abc import abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable
    from typing import Literal

    from ._acquire import AcquireReturnProxy


class ReadWriteBackend:
    """
    Backend contract for async reader/writer locks that share executor threads.

    :ivar lock_file: lock path used for contention errors
    :ivar timeout: default wait in seconds
    :ivar blocking: whether to wait on contention
    """

    __slots__ = ()

    lock_file: str
    timeout: float
    blocking: bool

    def _acquire_level_of(
        self,
        holder: int,
        mode: Literal["read", "write"],
        timeout: float,
        *,
        blocking: bool,
        cancel_check: Callable[[], bool],
    ) -> AcquireReturnProxy:
        # Executor threads can serve multiple task owners.
        return self._acquire(mode, timeout, blocking=blocking, holder=holder, cancel_check=cancel_check)

    @abstractmethod
    def _acquire(
        self,
        mode: Literal["read", "write"],
        timeout: float,
        *,
        blocking: bool,
        holder: int,
        cancel_check: Callable[[], bool],
    ) -> AcquireReturnProxy: ...

    @abstractmethod
    def _release_level_of(self, holder: int, /) -> None: ...

    @abstractmethod
    def _holds_level_of(self, holder: int, /) -> bool: ...

    @abstractmethod
    def close(self) -> None:
        """Release the backend hold and retire this instance."""


__all__ = [
    "ReadWriteBackend",
]
