from datetime import datetime, timedelta

from crfs_iq_recorder.listing_sort import (
    COL_DURATION,
    COL_MODIFIED,
    COL_NAME,
    COL_SIZE,
    COL_START,
    header_label,
    parse_date_folder_name,
    sort_entries,
    sort_entries_newest_first,
)
from crfs_iq_recorder.recording_history import new_recording
from crfs_iq_recorder.sftp_client import RemoteEntry


def _file(name: str, when: datetime, size: int = 100, folder: str = "20260913") -> RemoteEntry:
    return RemoteEntry(name, f"/mnt/1/remdata/{folder}/{name}", False, size, when)


def _dir(name: str, when: datetime | None = None) -> RemoteEntry:
    return RemoteEntry(name, f"/mnt/1/remdata/{name}", True, 0, when)


def test_parse_date_folder_name():
    assert parse_date_folder_name("20260913") == datetime(2026, 9, 13)
    assert parse_date_folder_name("20260913/") == datetime(2026, 9, 13)
    assert parse_date_folder_name("remdata") is None
    assert parse_date_folder_name("20261340") is None


def test_newest_first_uses_folder_date_and_file_times():
    now = datetime(2026, 9, 13, 12, 0, 0)
    entries = [
        _dir("20260115", now),
        _dir("20260913", datetime(2026, 1, 1)),
        _file("old.wav", now - timedelta(hours=2)),
        _file("new.wav", now),
        _file("mid.wav", now - timedelta(minutes=5)),
        RemoteEntry("orphan", "/mnt/1/remdata/orphan", True, 0, None),
    ]
    names = [item.name for item in sort_entries_newest_first(entries)]
    assert names[:5] == ["new.wav", "mid.wav", "old.wav", "20260913", "20260115"]
    assert names[-1] == "orphan"


def test_files_use_recording_timestamp_before_mtime():
    now = datetime(2026, 9, 13, 12, 0, 0)
    older_mtime = _file("iq_20260913_010000_0001.wav", now)
    newer_mtime = _file("later.wav", now + timedelta(hours=3))
    record = new_recording(
        host="192.0.2.10",
        start_hz=100,
        end_hz=200,
        center_hz=150,
        bandwidth_hz=100,
        duration_s=2,
        started_at=now + timedelta(hours=6),
    )
    ordered = sort_entries_newest_first(
        [older_mtime, newer_mtime],
        matched={older_mtime.path: record},
    )
    assert [item.name for item in ordered] == ["iq_20260913_010000_0001.wav", "later.wav"]


def test_column_sort_uses_bytes_and_keeps_missing_last():
    now = datetime(2026, 9, 13, 12, 0, 0)
    small = _file("small.wav", now, size=100)
    large = _file("large.wav", now, size=3_000_000_000)
    folder = _dir("20260913", now)
    names = [
        item.name
        for item in sort_entries([small, large, folder], column=COL_SIZE, descending=True)
    ]
    assert names[0] == "large.wav"
    assert names[1] == "small.wav"
    assert names[-1] == "20260913"


def test_column_sort_frequencies_and_duration_numeric():
    now = datetime(2026, 9, 13, 12, 0, 0)
    low = _file("low.wav", now)
    high = _file("high.wav", now)
    bare = _file("bare.wav", now)
    rec_low = new_recording(
        host="h",
        start_hz=100_000_000,
        end_hz=110_000_000,
        center_hz=105_000_000,
        bandwidth_hz=10_000_000,
        duration_s=1.5,
        started_at=now,
    )
    rec_high = new_recording(
        host="h",
        start_hz=800_000_000,
        end_hz=820_000_000,
        center_hz=810_000_000,
        bandwidth_hz=20_000_000,
        duration_s=40,
        started_at=now,
    )
    matched = {low.path: rec_low, high.path: rec_high}
    by_start = sort_entries(
        [bare, low, high],
        matched=matched,
        column=COL_START,
        descending=True,
    )
    assert [item.name for item in by_start] == ["high.wav", "low.wav", "bare.wav"]
    by_duration = sort_entries(
        [bare, high, low],
        matched=matched,
        column=COL_DURATION,
        descending=False,
    )
    assert [item.name for item in by_duration] == ["low.wav", "high.wav", "bare.wav"]


def test_name_and_modified_toggle():
    now = datetime(2026, 9, 13, 12, 0, 0)
    a = _file("alpha.wav", now - timedelta(hours=1))
    z = _file("zeta.wav", now)
    names = [item.name for item in sort_entries([z, a], column=COL_NAME, descending=False)]
    assert names == ["alpha.wav", "zeta.wav"]
    dates = [item.name for item in sort_entries([a, z], column=COL_MODIFIED, descending=True)]
    assert dates == ["zeta.wav", "alpha.wav"]


def test_header_label_arrows():
    assert header_label("Name", active=False, descending=True) == "Name"
    assert header_label("Modified", active=True, descending=True).endswith("▼")
    assert header_label("Modified ▼", active=True, descending=False).endswith("▲")
