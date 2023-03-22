from __future__ import annotations

from typing import Any


class Timeout(TimeoutError):
    """Raised when the lock could not be acquired in *timeout* seconds."""

    def __init__(self, lock_file: str) -> None:
        #: The path of the file lock.
        super().__init__(f"The file lock '{lock_file}' could not be acquired.")

        # Set filename so name of lock file is visible
        self.filename = lock_file

    def __reduce__(self) -> str | tuple[Any, ...]:
        # Properly pickle the exception
        return self.__class__, (self.filename,)

    def __str__(self) -> str:
        return self.args[0]

    @property
    def lock_file(self) -> str:
        # For compatibility
        return self.filename


__all__ = ["Timeout"]
