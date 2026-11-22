from __future__ import annotations

import os
from pathlib import Path

from filelock import StrictSoftFileLock


def initialize_protocol(lock_path: Path) -> None:
    with StrictSoftFileLock(lock_path):
        pass


def write_foreign_claim(claims: Path, token: str, state: str = "held") -> Path:
    claim = claims / f"{state}-v1-{token}.claim"
    claim.write_bytes(b"filelock-strict-v1\n" + token.encode() + f"\n{os.getpid()}\n686f7374\n4242\n".encode())
    return claim


def write_held_claim(claims: Path) -> Path:
    claims.mkdir(parents=True)
    claim = claims / f"held-v1-{'0' * 32}.claim"
    claim.write_bytes(b"filelock-strict-v1\n" + b"0" * 32 + b"\n1\n686f7374\n\n")
    return claim


def private_claim_path(lock_path: Path, token: str) -> Path:
    return Path(f"{lock_path}.filelock") / "claims" / f".intent-v1-{token}.claim.private-v1-{token}.tmp"


__all__ = [
    "initialize_protocol",
    "private_claim_path",
    "write_foreign_claim",
    "write_held_claim",
]
