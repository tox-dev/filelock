from __future__ import annotations

import os

import pytest

from filelock._soft._read_write._storage import encode_holder, holder_lease

from .conftest import FIRST, SECOND


def test_holder_record_keeps_its_length_across_nonces() -> None:
    first = encode_holder(FIRST, SECOND, 90.0)
    second = encode_holder(FIRST, FIRST, 90.0)
    assert len(first) == len(second)
    assert first != second
    assert f"pid={os.getpid()}".encode() in first


@pytest.mark.parametrize(
    "lease", [pytest.param(90.0, id="default"), pytest.param(1e-05, id="exponent"), pytest.param(10, id="integer")]
)
def test_holder_record_carries_its_lease(lease: float) -> None:
    assert holder_lease(encode_holder(FIRST, SECOND, lease)) == lease


@pytest.mark.parametrize(
    "record",
    [
        pytest.param(None, id="missing"),
        pytest.param(b"filelock-rw/1\nnonce=x\n", id="older-release"),
        pytest.param(b"\xff", id="non-ascii"),
        pytest.param(b"lease=abc\n", id="not-a-number"),
        pytest.param(b"lease=1_000.0\n", id="underscore"),
        pytest.param(b"lease= 1.0\n", id="padded"),
        pytest.param(b"lease=inf\n", id="infinite"),
        pytest.param(b"lease=0.0\n", id="zero"),
        pytest.param(b"lease=-1.0\n", id="negative"),
    ],
)
def test_holder_lease_ignores_what_it_cannot_trust(record: bytes | None) -> None:
    assert holder_lease(record) is None
