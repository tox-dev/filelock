from __future__ import annotations

from typing import Any


class Timeout(TimeoutError):  # noqa: N818
    """Raised when the lock could not be acquired in *timeout* seconds."""

    def __init__(self, lock_file: str) -> None:
        super().__init__()
        self._lock_file = lock_file

    def __reduce__(self) -> str | tuple[Any, ...]:
        return self.__class__, (self._lock_file,)  # Properly pickle the exception

    def __str__(self) -> str:
        return f"The file lock '{self._lock_file}' could not be acquired."

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}({self.lock_file!r})"

    @property
    def lock_file(self) -> str:
        """:return: The path of the file lock."""
        return self._lock_file


class FileLockDeadlockError(RuntimeError):
    """Raised when acquiring a lock would deadlock the current thread."""

    def __init__(self, lock_file: str) -> None:
        super().__init__()
        self._lock_file = lock_file

    def __reduce__(self) -> str | tuple[Any, ...]:
        return self.__class__, (self._lock_file,)

    def __str__(self) -> str:
        return (
            f"Acquiring lock on '{self._lock_file}' would deadlock: this file is already locked by another "
            f"FileLock instance in the current thread. Use is_singleton=True to enable cross-instance reentrant "
            f"locking, or reuse the existing FileLock object."
        )

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}({self.lock_file!r})"

    @property
    def lock_file(self) -> str:
        """:return: The path of the file lock."""
        return self._lock_file


__all__ = [
    "FileLockDeadlockError",
    "Timeout",
]
