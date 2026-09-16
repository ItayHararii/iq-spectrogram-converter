from datetime import datetime, timedelta

from crfs_iq_recorder.listing_sort import (
    COL_DURATION,
    COL_MODIFIED,
    COL_NAME,
    COL_SIZE,
    COL_START,
    build_listing_nodes,
    header_label,
    parse_date_folder_name,
    sort_entries,
    sort_entries_newest_first,
    sort_listing_nodes,
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


def test_split_parts_build_one_group_and_keep_single_files():
    now = datetime(2026, 9, 15, 14, 27, 18)
    part1 = _file("iq_20260915_150000_0001.wav", now, size=10)
    part2 = _file("iq_20260915_150000_0002.wav", now, size=20)
    lone = _file("iq_20260915_142800_0001.wav", now, size=5)
    plain = _file("iq-demo.wav", now, size=7)
    nodes = build_listing_nodes([part1, part2, lone, plain])
    groups = [node for node in nodes if node.kind == "group"]
    files = [node for node in nodes if node.kind == "file"]
    assert len(groups) == 1
    assert groups[0].name == "iq_20260915_150000"
    assert [entry.name for entry in groups[0].entries] == [
        "iq_20260915_150000_0001.wav",
        "iq_20260915_150000_0002.wav",
    ]
    assert {node.name for node in files} == {"iq_20260915_142800_0001.wav", "iq-demo.wav"}
    ordered = sort_listing_nodes(nodes)
    assert ordered[0].kind == "group"
    by_size = sort_listing_nodes(nodes, column=COL_SIZE, descending=True)
    assert by_size[0].kind == "group"
    assert sum(entry.size for entry in by_size[0].entries) == 30


def test_same_stem_in_different_folders_stays_separate():
    now = datetime(2026, 9, 15, 14, 27, 18)
    first = [
        RemoteEntry("iq_x_0001.wav", "/a/iq_x_0001.wav", False, 1, now),
        RemoteEntry("iq_x_0002.wav", "/a/iq_x_0002.wav", False, 1, now),
    ]
    second = [
        RemoteEntry("iq_x_0001.wav", "/b/iq_x_0001.wav", False, 1, now),
        RemoteEntry("iq_x_0002.wav", "/b/iq_x_0002.wav", False, 1, now),
    ]
    rec_a = new_recording(
        host="h",
        start_hz=1,
        end_hz=2,
        center_hz=1,
        bandwidth_hz=1,
        duration_s=1,
        started_at=now,
    )
    rec_b = new_recording(
        host="h",
        start_hz=3,
        end_hz=4,
        center_hz=3,
        bandwidth_hz=1,
        duration_s=2,
        started_at=now,
    )
    matched = {
        first[0].path: rec_a,
        first[1].path: rec_a,
        second[0].path: rec_b,
        second[1].path: rec_b,
    }
    nodes = build_listing_nodes(first + second, matched)
    groups = [node for node in nodes if node.kind == "group"]
    assert len(groups) == 2
    assert groups[0].group_key != groups[1].group_key
