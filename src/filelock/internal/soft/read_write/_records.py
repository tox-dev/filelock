from __future__ import annotations

__lazy_modules__ = ["filelock.internal.identity", "math", "secrets"]

import os
import secrets
from abc import abstractmethod
from math import isfinite
from typing import Final, Protocol

from filelock.internal.identity import host_name

PROTOCOL: Final[str] = "filelock-rw/1"
GENERATIONS_DIRECTORY: Final[str] = "gen"
HOLDERS_DIRECTORY: Final[str] = "holders"
TEMPORARY_PREFIX: Final[str] = ".commit-"
MAX_RECORD_SIZE: Final[int] = 1 << 20
MAX_LISTED_NAMES: Final[int] = 1 << 16
HOUSEKEEPING_BATCH: Final[int] = 1 << 8
_TOKEN_HEX_LENGTH: Final[int] = 32


class RecordFiles(Protocol):
    @abstractmethod
    def read(self, path: str) -> bytes | None: ...

    @abstractmethod
    def create(self, path: str, data: bytes, *, durable: bool = False) -> tuple[int, int]: ...

    @abstractmethod
    def link(self, source: str, target: str) -> bool: ...

    @abstractmethod
    def write_head(self, path: str, temporary: str, data: bytes) -> None: ...

    @abstractmethod
    def overwrite(self, path: str, data: bytes, identity: tuple[int, int], header: bytes) -> bool: ...

    @abstractmethod
    def unlink(self, path: str, *, witness: str | None = None) -> None: ...

    @abstractmethod
    def listdir(self, path: str) -> list[str]: ...

    @abstractmethod
    def prepare(self, root: str, *, force: bool = False) -> None: ...


def encode_holder(token: str, nonce: str, lease: float) -> bytes:
    # A peer with a shorter lease must still respect the writer's refresh interval.
    return holder_header(token) + (
        f"pid={os.getpid()}\nhost={host_name()}\nlease={float(lease)!r}\nnonce={nonce}\n"
    ).encode("ascii")


def holder_header(token: str) -> bytes:
    return f"{PROTOCOL}\ntoken={token}\n".encode("ascii")


def holder_lease(record: bytes | None) -> float | None:
    try:
        text = next(
            line for line in (record or b"").decode("ascii").split("\n") if line.startswith("lease=")
        ).removeprefix("lease=")
        lease = float(text)
    except (StopIteration, ValueError):  # UnicodeDecodeError is a ValueError
        return None
    # Accept only the spelling written by the holder.
    return lease if isfinite(lease) and lease > 0 and repr(lease) == text else None


def new_token() -> str:
    return secrets.token_hex(_TOKEN_HEX_LENGTH // 2)


def is_token(value: str) -> bool:
    return len(value) == _TOKEN_HEX_LENGTH and all(character in "0123456789abcdef" for character in value)


class TreeReplacedError(RuntimeError):
    pass


__all__ = [
    "GENERATIONS_DIRECTORY",
    "HOLDERS_DIRECTORY",
    "HOUSEKEEPING_BATCH",
    "MAX_LISTED_NAMES",
    "MAX_RECORD_SIZE",
    "PROTOCOL",
    "TEMPORARY_PREFIX",
    "RecordFiles",
    "TreeReplacedError",
    "encode_holder",
    "holder_header",
    "holder_lease",
    "is_token",
    "new_token",
]
