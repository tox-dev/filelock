from __future__ import annotations

import itertools
import os
import struct
from contextlib import closing
from typing import TYPE_CHECKING, Final, Literal

import pytest

pytest.importorskip("sqlite3")
import sqlite3
import string

from filelock import ReadWriteLock, Timeout
from tests.capability_marks import SKIP_ON_UNRELIABLE_PROCESS_SYNC

if TYPE_CHECKING:
    from pathlib import Path

pytestmark: Final[pytest.MarkDecorator] = SKIP_ON_UNRELIABLE_PROCESS_SYNC


@pytest.mark.timeout(60)
@pytest.mark.parametrize("mode", [pytest.param("read", id="read"), pytest.param("write", id="write")])
def test_read_write_lock_refuses_a_schema_that_never_finishes_loading(
    tmp_path: Path, mode: Literal["read", "write"]
) -> None:
    # Every interior page leads to the next one through each of its 660 cells, so the schema has 661**4 rows to scan.
    database = tmp_path / "lock.db"
    pages = [
        _btree_page(
            0x05,  # table interior page
            struct.pack(">IB", number + 1, 1),
            660,
            right_child=struct.pack(">I", number + 1),
            header_at=(100, 0, 0, 0)[number - 1],
        )
        for number in range(1, 5)
    ]
    pages.append(_btree_page(_TABLE_LEAF_PAGE, bytes([6, 1, 6, 0, 0, 0, 0, 0]), 1))
    pages[0][:100] = _file_header(len(pages))
    database.write_bytes(b"".join(pages))
    lock = ReadWriteLock(database, is_singleton=False)

    with pytest.raises(ValueError, match="schema"):
        getattr(lock, f"acquire_{mode}")(timeout=1)


def _btree_page(kind: int, cell: bytes, cell_count: int, *, right_child: bytes = b"", header_at: int = 0) -> bytearray:
    page = bytearray(_PAGE_SIZE)
    cell_at = _PAGE_SIZE - len(cell)
    page[cell_at:] = cell
    # Every slot of the cell pointer array names the one cell.
    header = struct.pack(">BHHHB", kind, 0, cell_count, cell_at, 0) + right_child
    pointers = struct.pack(">H", cell_at) * cell_count
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


@pytest.mark.parametrize("mode", [pytest.param("read", id="read"), pytest.param("write", id="write")])
def test_read_write_lock_refuses_a_schema_row_longer_than_a_lock_needs(
    tmp_path: Path, mode: Literal["read", "write"]
) -> None:
    # The row's text continues on an overflow page that names itself as the next one, so one page of disk reads as a
    # megabyte, far more schema text than a lock database holds and below SQLite's one-billion-byte string limit.
    long_sql_length = 1_000_000
    database = tmp_path / "lock.db"
    statement = b"CREATE TABLE application (value) --"
    columns = [b"table", b"application", b"application", b"\x02"]
    serial_types = [13 + 2 * len(column) for column in columns[:3]] + [1, 13 + 2 * long_sql_length]
    record_header = b"".join(_varint(serial_type) for serial_type in serial_types)
    record_start = _varint(len(record_header) + 1) + record_header + b"".join(columns) + statement
    record_length = len(record_start) + long_sql_length - len(statement)
    # How much of an overflowing record a table leaf keeps on its own page.
    local = (minimum := (_PAGE_SIZE - 12) * 32 // 255 - 23) + (record_length - minimum) % (_PAGE_SIZE - 4)
    cell = _varint(record_length) + _varint(1) + record_start.ljust(local, b"-") + struct.pack(">I", 2)
    leaf = _btree_page(_TABLE_LEAF_PAGE, cell, 1, header_at=100)
    page_count = long_sql_length // _PAGE_SIZE + 2
    leaf[:100] = _file_header(page_count)
    database.write_bytes(leaf + struct.pack(">I", 2).ljust(_PAGE_SIZE, b"-"))
    # SQLite refuses a record longer than the file, which a hole stretches at no cost.
    os.truncate(database, page_count * _PAGE_SIZE)
    lock = ReadWriteLock(database, is_singleton=False)

    with pytest.raises(ValueError, match="schema"):
        getattr(lock, f"acquire_{mode}")(timeout=1)


def _varint(value: int) -> bytes:
    groups = [value & 0x7F]
    while value := value >> 7:
        groups.insert(0, value & 0x7F | 0x80)
    return bytes(groups)


@pytest.mark.parametrize(
    ("rows", "unique_per_row", "row_length"),
    [
        pytest.param(100, 0, 0, id="many-tables"),
        pytest.param(20, 0, 4000, id="long-plain-rows"),
        pytest.param(20, 270, 4000, id="many-unique-constraints"),
    ],
)
def test_read_write_lock_refuses_large_application_schema(
    tmp_path: Path, rows: int, unique_per_row: int, row_length: int
) -> None:
    database = tmp_path / "lock.db"
    columns = string.ascii_lowercase
    constraints = "".join(
        f",UNIQUE({first},{second})"
        for first, second in itertools.islice(itertools.permutations(columns, 2), unique_per_row)
    )
    column_spec = f"{','.join(columns)}{constraints}".ljust(row_length)
    with closing(sqlite3.connect(database)) as connection:
        connection.executescript(";\n".join(f"CREATE TABLE t{number}({column_spec})" for number in range(rows)))

    with pytest.raises(ValueError, match="schema"):
        ReadWriteLock(database, is_singleton=False).acquire_read(timeout=1)


def test_read_write_lock_accepts_small_application_schema(tmp_path: Path) -> None:
    database = tmp_path / "lock.db"
    with closing(sqlite3.connect(database)) as connection:
        connection.executescript("".join(f"CREATE TABLE t{number}(value);" for number in range(20)))

    with (
        closing(ReadWriteLock(database, is_singleton=False)) as reader,
        closing(ReadWriteLock(database, is_singleton=False)) as writer,
        reader.read_lock(),
        pytest.raises(Timeout),
    ):
        writer.acquire_write(blocking=False)


_PAGE_SIZE: Final[int] = 4096


_TABLE_LEAF_PAGE: Final[int] = 0x0D
