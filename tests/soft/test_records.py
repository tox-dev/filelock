from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from filelock import OwnerRecord, SoftFileLease
from filelock._soft import encode_marker

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.parametrize(
    "record",
    [
        pytest.param("filelock/1\npid=1\nhost=h\nmode=lease\n", id="wrong-protocol"),
        pytest.param("filelock/2\npid=1\nhost=h\n", id="no-mode"),
        pytest.param("filelock/2\npid=1\nmode=lease\n", id="no-host"),
        pytest.param("filelock/2\npid=1\nhost=\nmode=lease\n", id="empty-host"),
        pytest.param("filelock/2\nhost=h\nmode=lease\n", id="no-pid"),
        pytest.param("filelock/2\npid=nine\nhost=h\nmode=lease\n", id="pid-not-a-number"),
        pytest.param("filelock/2\npid=0\nhost=h\nmode=lease\n", id="pid-below-range"),
        pytest.param("filelock/2\npid=99999999999\nhost=h\nmode=lease\n", id="pid-above-range"),
        pytest.param("filelock/2\npid=1\nhost=h\nmode=lease\nbare-line\n", id="field-without-value"),
        pytest.param("filelock/2\npid=1\nhost=h\nmode=lease\nduration=5\n", id="lease-without-token"),
        pytest.param("filelock/2\npid=1\nhost=h\nmode=lease\ntoken=t\n", id="lease-without-duration"),
        pytest.param("filelock/2\npid=1\nhost=h\nmode=lease\ntoken=t\nduration=0\n", id="lease-duration-zero"),
        pytest.param(
            "filelock/2\npid=1\nhost=h\nmode=lease\ntoken=t\nduration=soon\n", id="lease-duration-not-a-number"
        ),
        pytest.param("filelock/2\npid=1\nhost=h\nmode=lease\ntoken=t\nduration=nan\n", id="lease-duration-nan"),
        pytest.param("filelock/2\npid=1\nhost=h\nmode=lease\ntoken=t\nduration=inf\n", id="lease-duration-inf"),
        pytest.param("filelock/2\npid=1\nhost=h\nmode=lease\ntoken=t\nduration=5\nstart=later\n", id="start-nan"),
        pytest.param("filelock/2\npid=1\nhost=h\nmode=exclu", id="truncated-mode"),
        pytest.param("filelock/2\npid=1\nhost=h\nmode=lease\ntoken=t\nduration=1", id="truncated-duration"),
        pytest.param("filelock/2\npid=1_000\nhost=h\nmode=exclusive\n", id="pid-underscore"),
        pytest.param("filelock/2\npid=\u0661\u0662\u0663\nhost=h\nmode=exclusive\n", id="pid-arabic-indic-digits"),
        pytest.param("filelock/2\npid= 1\nhost=h\nmode=exclusive\n", id="pid-whitespace"),
        pytest.param("filelock/2\npid=+1\nhost=h\nmode=exclusive\n", id="pid-sign"),
        pytest.param("filelock/2\npid=1\nhost=h\nmode=exclusive\nstart=1_0\n", id="start-underscore"),
        pytest.param("filelock/2\npid=1\nhost=h\nmode=lease\ntoken=t\nduration= 1\n", id="duration-whitespace"),
        pytest.param("filelock/2\npid=1\nhost=h\nmode=lease\ntoken=t\nduration=1_0\n", id="duration-underscore"),
        pytest.param("filelock/2\npid=1\nhost=h\nmode=lease\ntoken=t\nduration=\u0665\n", id="duration-non-ascii"),
        pytest.param("filelock/2\npid=1\nhost=h\nmode=lease\ntoken=t\nduration=-1.0\n", id="duration-negative"),
        pytest.param("filelock/2\npid=1\nhost=h\nmode=lease\ntoken=t\nduration=1e400\n", id="duration-overflow"),
        pytest.param("filelock/2\npid=1\nhost=h\nmode=lease\ntoken=\nduration=1\n", id="lease-empty-token"),
    ],
)
def test_unreadable_record_names_no_owner(tmp_path: Path, record: str) -> None:
    marker = tmp_path / "a.lock"
    marker.write_text(record, encoding="utf-8")

    assert SoftFileLease(str(marker), lease_duration=1).owner is None


def test_encode_marker_omits_absent_lease_fields() -> None:
    rendered = encode_marker(OwnerRecord(pid=7, hostname="h", mode="unknown"))

    assert rendered == b"filelock/2\npid=7\nhost=h\nmode=unknown\n"


@pytest.mark.parametrize("duration", [0.9, 30, 30.0, 1e-05, 1.5e20])
def test_record_duration_reads_back(marker: Path, duration: float) -> None:
    record = OwnerRecord(4242, "h", "lease", "t", duration)
    marker.write_bytes(encode_marker(record))

    assert SoftFileLease(str(marker), lease_duration=1).owner == record


def test_record_start_token_reads_back(tmp_path: Path) -> None:
    marker = tmp_path / "a.lock"
    record = "filelock/2\npid=4242\nhost=somehost\nmode=lease\ntoken=t\nduration=5\nstart=987654\n"
    marker.write_text(record, encoding="utf-8")

    owner = SoftFileLease(str(marker), lease_duration=1).owner

    assert owner is not None
    assert (owner.pid, owner.hostname, owner.mode, owner.start) == (4242, "somehost", "lease", 987654)


def test_record_from_a_newer_filelock_still_names_its_owner(tmp_path: Path) -> None:
    marker = tmp_path / "a.lock"
    record = "filelock/2\npid=4242\nhost=somehost\nmode=lease\ntoken=t\nduration=5\nstart-id=17\n"
    marker.write_text(record, encoding="utf-8")

    owner = SoftFileLease(str(marker), lease_duration=1).owner

    assert owner is not None
    assert (owner.pid, owner.hostname, owner.mode) == (4242, "somehost", "lease")


@pytest.mark.parametrize("published", ["strict", "banana"])
def test_record_with_an_unknown_mode_names_its_owner(tmp_path: Path, published: str) -> None:
    marker = tmp_path / "a.lock"
    marker.write_text(f"filelock/2\npid=4242\nhost=somehost\nmode={published}\n", encoding="utf-8")

    owner = SoftFileLease(str(marker), lease_duration=1).owner

    assert owner is not None
    assert (owner.pid, owner.hostname, owner.mode) == (4242, "somehost", "unknown")
