from __future__ import annotations

import os
import struct
from typing import TYPE_CHECKING, Final, Literal

import pytest

pytest.importorskip("sqlite3")

from filelock import ReadWriteLock

if TYPE_CHECKING:
    from pathlib import Path

# https://www.sqlite.org/fileformat.html
_PAGE_SIZE: Final = 4096
_TABLE_INTERIOR_PAGE: Final = 0x05
_TABLE_LEAF_PAGE: Final = 0x0D
# Far longer than any schema text a lock database holds, yet below SQLite's own one-billion-byte string limit.
_LONG_SQL_LENGTH: Final = 1_000_000


@pytest.mark.timeout(60)
@pytest.mark.parametrize("mode", [pytest.param("read", id="read"), pytest.param("write", id="write")])
def test_read_write_lock_refuses_a_schema_that_never_finishes_loading(
    tmp_path: Path, mode: Literal["read", "write"]
) -> None:
    # Every interior page leads to the next one through each of its cells, so the schema has 661**4 rows to scan.
    database: Final = tmp_path / "lock.db"
    pages: Final = [
        _btree_page(
            _TABLE_INTERIOR_PAGE,
            struct.pack(">IB", number + 1, 1),
            660,
            right_child=struct.pack(">I", number + 1),
            header_at=100 if number == 1 else 0,
        )
        for number in range(1, 5)
    ]
    pages.append(_btree_page(_TABLE_LEAF_PAGE, bytes([6, 1, 6, 0, 0, 0, 0, 0]), 1))
    pages[0][:100] = _file_header(len(pages))
    database.write_bytes(b"".join(pages))
    lock: Final = ReadWriteLock(database, is_singleton=False)

    with pytest.raises(ValueError, match="schema"):
        (lock.acquire_read if mode == "read" else lock.acquire_write)(timeout=1)


@pytest.mark.parametrize("mode", [pytest.param("read", id="read"), pytest.param("write", id="write")])
def test_read_write_lock_refuses_a_schema_row_longer_than_a_lock_needs(
    tmp_path: Path, mode: Literal["read", "write"]
) -> None:
    # The row's text continues on an overflow page that names itself as the next one, so a page of disk reads as
    # _LONG_SQL_LENGTH bytes.
    database: Final = tmp_path / "lock.db"
    statement: Final = b"CREATE TABLE application (value) --"
    columns: Final = [b"table", b"application", b"application", b"\x02"]
    serial_types: Final = [13 + 2 * len(column) for column in columns[:3]] + [1, 13 + 2 * _LONG_SQL_LENGTH]
    record_header: Final = b"".join(_varint(serial_type) for serial_type in serial_types)
    record_start: Final = _varint(len(record_header) + 1) + record_header + b"".join(columns) + statement
    record_length: Final = len(record_start) + _LONG_SQL_LENGTH - len(statement)
    # How much of an overflowing record a table leaf keeps on its own page.
    local: Final = (minimum := (_PAGE_SIZE - 12) * 32 // 255 - 23) + (record_length - minimum) % (_PAGE_SIZE - 4)
    cell: Final = _varint(record_length) + _varint(1) + record_start.ljust(local, b"-") + struct.pack(">I", 2)
    leaf: Final = _btree_page(_TABLE_LEAF_PAGE, cell, 1, header_at=100)
    page_count: Final = _LONG_SQL_LENGTH // _PAGE_SIZE + 2
    leaf[:100] = _file_header(page_count)
    database.write_bytes(leaf + struct.pack(">I", 2).ljust(_PAGE_SIZE, b"-"))
    # SQLite refuses a record longer than the file, which a hole stretches at no cost.
    os.truncate(database, page_count * _PAGE_SIZE)
    lock: Final = ReadWriteLock(database, is_singleton=False)

    with pytest.raises(ValueError, match="schema"):
        (lock.acquire_read if mode == "read" else lock.acquire_write)(timeout=1)


def _btree_page(kind: int, cell: bytes, cell_count: int, *, right_child: bytes = b"", header_at: int = 0) -> bytearray:
    page: Final = bytearray(_PAGE_SIZE)
    cell_at: Final = _PAGE_SIZE - len(cell)
    page[cell_at:] = cell
    # Every slot of the cell pointer array names the one cell.
    header: Final = struct.pack(">BHHHB", kind, 0, cell_count, cell_at, 0) + right_child
    pointers: Final = struct.pack(">H", cell_at) * cell_count
    page[header_at : header_at + len(header) + len(pointers)] = header + pointers
    return page


def _file_header(page_count: int) -> bytes:
    # Rollback journal format, a matching change counter and version-valid-for, so SQLite trusts the page count.
    return struct.pack(
        ">16sH6B12I20s2I",
        b"SQLite format 3\0",
        _PAGE_SIZE,
        *(1, 1, 0, 64, 32, 32),
        *(1, page_count, 0, 0, 1, 4, 0, 0, 1, 0, 0, 0),
        b"",
        1,
        3040001,
    )


def _varint(value: int) -> bytes:
    groups: Final = [value & 0x7F]
    while value := value >> 7:
        groups.insert(0, value & 0x7F | 0x80)
    return bytes(groups)
