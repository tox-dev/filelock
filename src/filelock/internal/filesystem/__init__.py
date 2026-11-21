"""Filesystem operations shared by lock backends."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from importlib.machinery import ModuleSpec

__spec__ = cast("ModuleSpec", __spec__)

__lazy_modules__ = [f"{__spec__.parent}._exchange", f"{__spec__.parent}._files", f"{__spec__.parent}._ownership"]

from ._exchange import break_lock_file, exchange_back, exchange_names, exchange_supported, remove_own_file, unlink_path
from ._files import (
    ensure_directory_exists,
    open_into,
    pin_inode,
    raise_on_not_writable_file,
    touch,
    write_all,
    write_at_start,
)
from ._ownership import foreign_owner

__all__ = [
    "break_lock_file",
    "ensure_directory_exists",
    "exchange_back",
    "exchange_names",
    "exchange_supported",
    "foreign_owner",
    "open_into",
    "pin_inode",
    "raise_on_not_writable_file",
    "remove_own_file",
    "touch",
    "unlink_path",
    "write_all",
    "write_at_start",
]
